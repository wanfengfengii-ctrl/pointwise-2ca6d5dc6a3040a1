import json
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.importer import ingest
from archive.session import open_archive
from demo import load_reel
from reader.cursor import next as read_next
from reader.tasks import open as open_task
from reports.issue import report
from revision.publication import amend


ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_legacy(workspace, reopen, fixture):
    reel = ingest(workspace, *load_reel(ROOT / fixture["source"]))
    task_a = open_task(workspace, reel.reel_id)
    prefix = read_next(workspace, task_a.task_id, fixture["a_prefix"])

    def submit(name, **kwargs):
        request = fixture["requests"][name]
        replacements = [
            (block_no, value.to_bytes(reel.layout.block_width, "little", signed=True))
            for block_no, value in request["readings"]
        ]
        return amend(workspace, reel.reel_id, request["request_id"], request["parent_version"],
                     replacements, **kwargs)

    events = []
    receipt = submit("tail", checkpoint=events.append)
    require(receipt.version == 2, "ordinary reel did not publish its first amendment")
    require(events[:2] == ["first_payload_synced", "publication_committed"],
            "ordinary publication did not report both checkpoints in order")
    try:
        submit("head")
    except ValueError as error:
        require("unfinalized" in str(error), f"unexpected ordinary rejection: {error}")
        gate = str(error)
    else:
        raise RuntimeError("ordinary reel accepted a second unfinalized amendment")

    workspace = reopen()
    task_b = open_task(workspace, reel.reel_id)
    require((task_a.version, task_b.version) == (1, 2), "ordinary version boundary moved")
    results = []
    for task, already_read in ((task_a, prefix), (task_b, ())):
        version = str(task.version)
        readings = [item.value for item in already_read + read_next(workspace, task.task_id, reel.layout.block_count)]
        require(readings == fixture["expected"]["readings"][version], f"wrong readings for version {version}")
        sealed = report(workspace, task_id=task.task_id, threshold=fixture["threshold"])
        require([row["block_no"] for row in sealed["hits"]] == fixture["expected"]["hit_blocks"][version],
                f"wrong report hits for version {version}")
        results.append(sealed)

    workspace = reopen()
    for sealed in results:
        require(report(workspace, report_id=sealed["report_id"]) == sealed, "sealed report changed after reopen")
    return {"ordinary_gate": gate, "checkpoints": events, "reports": results}


def main():
    fixture = json.loads((ROOT / "examples" / "slot_reuse.json").read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="scan-reel-slot-baseline-") as root:
        def reopen():
            return open_archive(root)

        result = check_legacy(reopen(), reopen, fixture)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
