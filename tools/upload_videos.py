#!/usr/bin/env python3
"""Upload concept animation videos to Supabase Storage.

Prerequisites:
  1. Create a PUBLIC bucket named 'concept-animations' in your Supabase dashboard:
     Storage → New Bucket → Name: concept-animations → Public: ON
  2. Set env vars: SUPABASE_URL, SUPABASE_KEY (use service_role key for uploads)

Usage:
  python tools/upload_videos.py
"""
import os
import sys
import json
import urllib.request
import urllib.error

BUCKET = "concept-animations"
VIDEO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "media", "concept_animations", "draft", "videos",
)

# scene_dir -> final mp4 filename
SCENES = {
    "scene_s01_grid": "S01GridConcept.mp4",
    "scene_s02_state_machine": "S02StateMachine.mp4",
    "scene_s03_slots_autoscale": "S03SlotsAutoscale.mp4",
    "scene_s04_rangers": "S04Rangers.mp4",
    "scene_s05_hmm": "S05HMM.mp4",
    "scene_s06_consensus": "S06Consensus.mp4",
    "scene_s07_training_quality": "S07TrainingQuality.mp4",
    "scene_s08_mts": "S08MTS.mp4",
    "scene_s09_bocpd": "S09BOCPD.mp4",
    "scene_s10_throughput": "S10Throughput.mp4",
    "scene_s11_survival": "S11Survival.mp4",
    "scene_s12_stats": "S12Stats.mp4",
    "scene_s13_digest": "S13Digest.mp4",
    "scene_s14_ai_advisor": "S14AIAdvisor.mp4",
    "scene_s15_main_loop": "S15MainLoop.mp4",
    "scene_s16_capacity": "S16Capacity.mp4",
    "scene_s17_accumulation": "S17Accumulation.mp4",
    "scene_s18_full_factory": "S18FullFactory.mp4",
}


def upload_file(supabase_url: str, key: str, local_path: str, remote_name: str) -> bool:
    """Upload a single file to Supabase Storage. Returns True on success."""
    url = f"{supabase_url}/storage/v1/object/{BUCKET}/{remote_name}"
    with open(local_path, "rb") as f:
        data = f.read()

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("apikey", key)
    req.add_header("Content-Type", "video/mp4")
    req.add_header("x-upsert", "true")  # overwrite if exists

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        print(f"  OK  {remote_name} ({len(data):,} bytes) -> {resp.status}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  FAIL {remote_name} -> {e.code}: {body}")
        return False


def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_KEY", "")

    if not supabase_url or not supabase_key:
        print("ERROR: Set SUPABASE_URL and SUPABASE_KEY env vars first.")
        print("  Tip: Use the service_role key (not anon) for storage uploads.")
        sys.exit(1)

    if not os.path.isdir(VIDEO_DIR):
        print(f"ERROR: Video directory not found: {VIDEO_DIR}")
        sys.exit(1)

    print(f"Supabase: {supabase_url}")
    print(f"Bucket:   {BUCKET}")
    print(f"Videos:   {VIDEO_DIR}")
    print()

    ok_count = 0
    fail_count = 0

    for scene_dir, filename in sorted(SCENES.items()):
        local_path = os.path.join(VIDEO_DIR, scene_dir, "480p15", filename)
        if not os.path.isfile(local_path):
            print(f"  SKIP {filename} (not found: {local_path})")
            fail_count += 1
            continue
        if upload_file(supabase_url, supabase_key, local_path, filename):
            ok_count += 1
        else:
            fail_count += 1

    print()
    print(f"Done: {ok_count} uploaded, {fail_count} failed")

    if ok_count > 0:
        print()
        print("Public URL pattern:")
        sample = list(SCENES.values())[0]
        print(f"  {supabase_url}/storage/v1/object/public/{BUCKET}/{sample}")


if __name__ == "__main__":
    main()
