"""
Test script: Run a single round of crawling for all sources.
Clears existing results per-source before each run to ensure clean testing.
Usage: python test_round.py [round_number]
"""
import sys
import time
import json
import urllib.request
import urllib.error

BASE = "http://localhost:8090"
SOURCE_IDS = [1, 2, 3, 4, 5, 6, 7, 11]
POLL_INTERVAL = 15  # seconds
MAX_WAIT = 600  # 10 minutes per source


def api_get(path):
    """GET request, return parsed JSON."""
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def api_post(path, data=None):
    """POST request with JSON body."""
    body = json.dumps(data).encode() if data else b""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def api_delete(path):
    """DELETE request."""
    req = urllib.request.Request(f"{BASE}{path}", method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def clear_source_results(source_id):
    """Delete all results for a source."""
    results = api_get(f"/api/results?source_id={source_id}&limit=500")
    if not results:
        return 0
    ids = [r["id"] for r in results]
    if ids:
        resp = api_post("/api/results/batch-delete", {"ids": ids})
        return resp.get("deleted", 0)
    return 0


def wait_for_completion(max_wait=MAX_WAIT):
    """Wait until no sources are running."""
    start = time.time()
    while time.time() - start < max_wait:
        status = api_get("/api/tasks/running")
        if not status.get("running"):
            return True
        running_ids = status.get("running_source_ids", [])
        elapsed = int(time.time() - start)
        print(f"  [{elapsed}s] Running: source_ids={running_ids}", flush=True)
        time.sleep(POLL_INTERVAL)
    return False


def get_latest_task_for_source(source_id):
    """Get the most recent task for a source."""
    tasks = api_get("/api/tasks?limit=20")
    for t in tasks:
        if t.get("source_id") == source_id:
            return t
    return None


def analyze_results(source_id):
    """Analyze results for a source."""
    results = api_get(f"/api/results?source_id={source_id}&limit=100")
    if not results:
        return {"count": 0, "titles": [], "types": {}, "has_summary": 0, "has_tags": 0}

    types = {}
    has_summary = 0
    has_tags = 0
    for r in results:
        ct = r.get("content_type", "news")
        types[ct] = types.get(ct, 0) + 1
        if r.get("summary"):
            has_summary += 1
        if r.get("tags"):
            has_tags += 1

    return {
        "count": len(results),
        "titles": [r["title"][:60] for r in results[:3]],
        "types": types,
        "has_summary": has_summary,
        "has_tags": has_tags,
    }


def main():
    round_num = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print(f"\n{'='*60}")
    print(f"  ROUND {round_num} TEST")
    print(f"{'='*60}\n")

    # Check server
    try:
        api_get("/api/tasks/running")
    except Exception as e:
        print(f"ERROR: Server not reachable: {e}")
        sys.exit(1)

    # Clear section history to avoid stale skipping
    try:
        api_post("/api/tasks/clear-section-history")
        print("Section history cleared.\n")
    except Exception:
        print("Warning: could not clear section history.\n")

    results_summary = {}

    for sid in SOURCE_IDS:
        # Get source name
        sources = api_get("/api/sources")
        src = next((s for s in sources if s["id"] == sid), None)
        src_name = src["name"] if src else f"Source {sid}"

        print(f"\n--- Source {sid}: {src_name} ---")

        # Step 1: Clear existing results
        cleared = clear_source_results(sid)
        print(f"  Cleared {cleared} existing results")

        # Step 2: Trigger crawl
        try:
            resp = api_post("/api/tasks/trigger", {"source_ids": [sid]})
            print(f"  Triggered: batch={resp.get('batch_id', '?')}")
        except Exception as e:
            print(f"  ERROR triggering: {e}")
            results_summary[sid] = {"name": src_name, "count": -1, "error": str(e)}
            continue

        # Step 3: Wait for completion
        time.sleep(3)  # brief pause before polling
        completed = wait_for_completion()
        if not completed:
            print(f"  WARNING: Timed out after {MAX_WAIT}s")

        # Step 4: Analyze results
        analysis = analyze_results(sid)
        task = get_latest_task_for_source(sid)
        task_status = task.get("status", "?") if task else "?"

        print(f"  Status: {task_status}")
        print(f"  Items: {analysis['count']}")
        print(f"  Types: {analysis['types']}")
        print(f"  Summaries: {analysis['has_summary']}/{analysis['count']}")
        print(f"  Tags: {analysis['has_tags']}/{analysis['count']}")
        if analysis["titles"]:
            print(f"  Sample titles:")
            for t in analysis["titles"]:
                print(f"    - {t}")

        results_summary[sid] = {
            "name": src_name,
            "count": analysis["count"],
            "types": analysis["types"],
            "summary_pct": f"{analysis['has_summary']}/{analysis['count']}",
            "tags_pct": f"{analysis['has_tags']}/{analysis['count']}",
        }

    # Final summary table
    print(f"\n\n{'='*60}")
    print(f"  ROUND {round_num} SUMMARY")
    print(f"{'='*60}")
    print(f"{'ID':>4} {'Source':<20} {'Items':>6} {'Summary':>10} {'Tags':>10} {'Types'}")
    print(f"{'-'*4} {'-'*20} {'-'*6} {'-'*10} {'-'*10} {'-'*30}")
    for sid in SOURCE_IDS:
        r = results_summary.get(sid, {})
        name = r.get("name", "?")[:20]
        count = r.get("count", -1)
        summary = r.get("summary_pct", "?")
        tags = r.get("tags_pct", "?")
        types = str(r.get("types", {}))
        print(f"{sid:>4} {name:<20} {count:>6} {summary:>10} {tags:>10} {types}")

    print()


if __name__ == "__main__":
    main()
