import concurrent.futures
import importlib.machinery
import types
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests

try:
    import inquirer
except Exception:
    inquirer = None


DEFAULT_HEADERS = {
    "accept": "*/*",
    "user-agent": "Mozilla/5.0",
}

SCRIPT_DIR = Path(__file__).resolve().parent
PARSER_PATH = SCRIPT_DIR / "ism_parser.py"

TIMEOUT = 30
CHUNK_SIZE = 1024 * 256
DOWNLOAD_PROGRESS_WIDTH = 40
PROBE_WORKERS = 16
VIDEO_PREPROBE_LIMIT = None

try:
    if not PARSER_PATH.exists():
        raise FileNotFoundError(f"ism_parser.py was not found at: {PARSER_PATH}")

    loader = importlib.machinery.SourceFileLoader("ism_parser_module", str(PARSER_PATH))
    parser_module = types.ModuleType(loader.name)
    parser_module.__file__ = str(PARSER_PATH)
    loader.exec_module(parser_module)

    if not hasattr(parser_module, "write_piff_header"):
        raise RuntimeError("ism_parser.py does not expose write_piff_header")

    url = input("Enter your URL: ").strip()
    if not url:
        raise ValueError("A manifest URL is required")

    headers = dict(DEFAULT_HEADERS)

    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    manifest_text = response.text

    xml_text = manifest_text.lstrip("\ufeff").strip()
    root = ET.fromstring(xml_text)
    if root.tag != "SmoothStreamingMedia":
        raise ValueError("The response is not a Smooth Streaming manifest")

    protection = root.find("Protection")
    pssh = None
    if protection is not None:
        header = protection.find("ProtectionHeader")
        if header is not None and header.text is not None:
            value = header.text.strip()
            pssh = value or None

    tracks = []
    next_track_id = 1
    manifest_duration = int(root.get("Duration", "0"))

    for stream_index in root.findall("StreamIndex"):
        media_type = (stream_index.get("Type") or "").lower()
        if media_type not in {"video", "audio"}:
            continue

        url_template = stream_index.get("Url")
        if not url_template:
            continue

        chunk_times = []
        current_time = None

        for chunk in stream_index.findall("c"):
            t_attr = chunk.get("t")
            d_attr = chunk.get("d")
            r_attr = chunk.get("r")

            if d_attr is None:
                raise ValueError("Each <c> chunk must contain a duration 'd'")

            duration = int(d_attr)
            repeat = int(r_attr) if r_attr is not None else 0

            if t_attr is not None:
                current_time = int(t_attr)
            elif current_time is None:
                current_time = 0

            chunk_times.append(current_time)

            for _ in range(repeat):
                current_time += duration
                chunk_times.append(current_time)

            current_time += duration

        stream_name = stream_index.get("Name") or media_type
        language = stream_index.get("Language") or "und"
        scheme = stream_index.get("ProtectionScheme") or ("cenc" if pssh else "-")

        for quality in stream_index.findall("QualityLevel"):
            codec = (
                quality.get("FourCC")
                or quality.get("Codec")
                or quality.get("Subtype")
                or ("AAC" if media_type == "audio" else "avc1")
            )

            track = {
                "track_id": next_track_id,
                "media_type": media_type,
                "stream_name": stream_name,
                "language": language,
                "url_template": url_template,
                "chunk_times": chunk_times,
                "bitrate": int(quality.get("Bitrate", "0") or 0),
                "codec": codec,
                "codec_private_data": quality.get("CodecPrivateData", "") or "",
                "timescale": int(root.get("TimeScale", stream_index.get("TimeScale", "10000000"))),
                "duration": manifest_duration,
                "pssh": pssh,
                "scheme": quality.get("ProtectionScheme") or scheme,
                "width": int(quality.get("MaxWidth", stream_index.get("MaxWidth", quality.get("Width", "0")) or 0)),
                "height": int(quality.get("MaxHeight", stream_index.get("MaxHeight", quality.get("Height", "0")) or 0)),
                "channels": int(quality.get("Channels", "2") or 2),
                "bits": int(quality.get("BitsPerSample", "16") or 16),
                "sample_rate": int(quality.get("SamplingRate", "48000") or 48000),
                "nal_unit_length_field": 4,
                "is_drm_protected": pssh is not None,
                "estimated_real_bitrate": None,
                "estimated_from_fragments": False,
                "probe_complete": False,
                "probe_error": None,
            }
            tracks.append(track)
            next_track_id += 1

    video_tracks = [track for track in tracks if track["media_type"] == "video"]
    if VIDEO_PREPROBE_LIMIT is not None:
        video_tracks = video_tracks[:VIDEO_PREPROBE_LIMIT]

    if video_tracks:
        print("[+] Measuring video variants before the menu")
        total_videos = len(video_tracks)
        last_probe_line_length = 0

        for index, track in enumerate(video_tracks, start=1):
            codec = str(track.get("codec") or "unknown")
            if track.get("width") and track.get("height"):
                size = f"{track['width']}x{track['height']}"
            else:
                size = "unknown size"

            probe_line = f"[+] Pre-probing videos: {index}/{total_videos} ({codec} {size})"
            padding = " " * max(0, last_probe_line_length - len(probe_line))
            print("\r" + probe_line + padding, end="", flush=True)
            last_probe_line_length = len(probe_line)

            chunk_times = track.get("chunk_times") or []
            if not chunk_times:
                track["probe_complete"] = True
                track["probe_error"] = "No chunks found"
                continue

            parsed = list(urlparse(url))
            parsed[4] = ""
            parsed[5] = ""
            manifest_base = urlunparse(parsed)
            if "/" in manifest_base:
                manifest_base = manifest_base.rsplit("/", 1)[0] + "/"

            fragment_urls = []
            for start_time_value in chunk_times:
                template = track["url_template"]
                if "{bitrate}" in template:
                    template = template.replace("{bitrate}", str(track["bitrate"]))
                if "{start time}" in template:
                    template = template.replace("{start time}", str(start_time_value))
                if "{start_time}" in template:
                    template = template.replace("{start_time}", str(start_time_value))
                fragment_urls.append(manifest_base + template)

            measured = 0
            total_bytes = 0

            with requests.Session() as probe_session:
                probe_session.headers.update(headers)

                with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
                    future_map = {}

                    for fragment_url in fragment_urls:
                        future = executor.submit(
                            lambda session, fragment: (
                                (lambda:
                                    (
                                        (lambda response:
                                            int(response.headers.get("Content-Length"))
                                            if response.headers.get("Content-Length", "").isdigit()
                                            else None
                                        )(session.head(fragment, timeout=TIMEOUT, allow_redirects=True))
                                    )
                                )()
                            ),
                            probe_session,
                            fragment_url,
                        )
                        future_map[future] = fragment_url

                    for future in concurrent.futures.as_completed(future_map):
                        fragment_url = future_map[future]
                        size = 0

                        try:
                            value = future.result()
                            if value:
                                size = int(value)
                        except Exception:
                            size = 0

                        if size <= 0:
                            try:
                                range_response = probe_session.get(
                                    fragment_url,
                                    headers={"Range": "bytes=0-0"},
                                    stream=True,
                                    timeout=TIMEOUT,
                                    allow_redirects=True,
                                )
                                range_response.raise_for_status()
                                content_range = range_response.headers.get("Content-Range", "")
                                match = re.search(r"/(\d+)$", content_range)
                                if match:
                                    size = int(match.group(1))
                                elif range_response.headers.get("Content-Length", "").isdigit():
                                    size = int(range_response.headers.get("Content-Length"))
                            except Exception:
                                size = 0

                        if size <= 0:
                            try:
                                full_response = probe_session.get(
                                    fragment_url,
                                    stream=True,
                                    timeout=TIMEOUT,
                                    allow_redirects=True,
                                )
                                full_response.raise_for_status()
                                if full_response.headers.get("Content-Length", "").isdigit():
                                    size = int(full_response.headers.get("Content-Length"))
                                else:
                                    streamed_total = 0
                                    for piece in full_response.iter_content(chunk_size=CHUNK_SIZE):
                                        if piece:
                                            streamed_total += len(piece)
                                    size = streamed_total
                            except Exception:
                                size = 0

                        if size > 0:
                            measured += 1
                            total_bytes += size

            track["probe_complete"] = True

            if measured > 0:
                if measured == len(fragment_urls):
                    estimated_total_bytes = total_bytes
                else:
                    estimated_total_bytes = (total_bytes / measured) * len(fragment_urls)

                duration_seconds = 0.0
                if int(track.get("duration") or 0) > 0 and int(track.get("timescale") or 0) > 0:
                    duration_seconds = int(track["duration"]) / int(track["timescale"])

                if duration_seconds > 0:
                    estimated_kbps = int((estimated_total_bytes * 8) / duration_seconds / 1000)
                    track["estimated_real_bitrate"] = estimated_kbps
                    track["estimated_from_fragments"] = True
                else:
                    track["probe_error"] = "Invalid duration/timescale"
            else:
                track["probe_error"] = "Could not measure fragment sizes"

        print("\r" + " " * last_probe_line_length, end="", flush=True)
        print("\r[+] Video bitrate pre-probing finished")

    print(f"[+] Found {len(tracks)} downloadable track variants")

    if not tracks:
        raise RuntimeError("No audio or video tracks were found in the manifest")

    def_describe_disabled = True  # marker only, no defs used

    descriptions = []
    for track in tracks:
        if track.get("estimated_from_fragments") and track.get("estimated_real_bitrate"):
            bitrate_text = f"real≈{track['estimated_real_bitrate']} kbps"
        else:
            bitrate_text = f"{track['bitrate'] // 1000 if track['bitrate'] else 0} kbps"

        if track["media_type"] == "video":
            size = f"{track['width']}x{track['height']}" if track["width"] and track["height"] else "unknown size"
            label = f"VIDEO | {track['codec']} | {size} | {bitrate_text} | chunks={len(track['chunk_times'])}"
        else:
            label = (
                f"AUDIO | {track['language']} | {track['codec']} | "
                f"{track['channels']}ch | {track['sample_rate']} Hz | {bitrate_text} | chunks={len(track['chunk_times'])}"
            )
        descriptions.append(label)

    if inquirer is not None:
        choices = [(descriptions[index], index) for index in range(len(tracks))]
        answer = inquirer.prompt([inquirer.List("track_index", message="Select the track to download", choices=choices)])
        if not answer:
            raise KeyboardInterrupt
        selected_track = tracks[int(answer["track_index"])]
        selected_description = descriptions[int(answer["track_index"])]
    else:
        print("[?] Select the track to download:")
        for index, label in enumerate(descriptions, start=1):
            prefix = ">" if index == 1 else " "
            print(f" {prefix} {index:03d}. {label}")

        while True:
            raw = input("Enter track number [1]: ").strip() or "1"
            if raw.isdigit() and 1 <= int(raw) <= len(tracks):
                selected_track = tracks[int(raw) - 1]
                selected_description = descriptions[int(raw) - 1]
                break
            print("[!] Invalid selection")

    print(f"[+] Selected: {selected_description}")

    stem_parts = [selected_track["media_type"], str(selected_track.get("codec") or "track")]
    if selected_track["media_type"] == "video":
        if selected_track.get("width") and selected_track.get("height"):
            stem_parts.append(f"{selected_track['width']}x{selected_track['height']}")
        extension = ".mp4"
    else:
        stem_parts.append(selected_track.get("language") or "und")
        extension = ".m4a"

    default_name = "_".join(stem_parts)
    default_name = re.sub(r"[\\/:*?\"<>|]+", "_", default_name)
    default_name = re.sub(r"\s+", "_", default_name).strip("._ ")
    if not default_name:
        default_name = f"output_{uuid.uuid4().hex[:8]}"
    default_name += extension

    output_name = input(f"Output file name [{default_name}]: ").strip() or default_name
    output_path = Path(output_name).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest_copy_name = re.sub(r"[\\/:*?\"<>|]+", "_", output_path.stem + "_manifest.xml")
    manifest_copy_name = re.sub(r"\s+", "_", manifest_copy_name).strip("._ ")
    if not manifest_copy_name:
        manifest_copy_name = f"manifest_{uuid.uuid4().hex[:8]}.xml"
    manifest_copy_path = output_path.with_name(manifest_copy_name)
    manifest_copy_path.write_text(manifest_text, encoding="utf-8")

    print("Detected tracks:")
    encrypted = "yes" if selected_track.get("is_drm_protected") else "no"
    entry = str(selected_track.get("codec") or "unknown").lower()
    handler = "vide" if selected_track["media_type"] == "video" else "soun"
    scheme = "-"
    if selected_track.get("is_drm_protected"):
        scheme = str(selected_track.get("scheme") or "cenc")

    print(
        f"  track={selected_track['track_id']} "
        f"handler={handler} "
        f"entry={entry} "
        f"encrypted={encrypted} "
        f"scheme={scheme}"
    )
    print(f"[+] Output: {output_path}")

    chunk_times = selected_track.get("chunk_times") or []
    if not chunk_times:
        raise RuntimeError("The selected track does not contain chunks")

    parsed = list(urlparse(url))
    parsed[4] = ""
    parsed[5] = ""
    manifest_base = urlunparse(parsed)
    if "/" in manifest_base:
        manifest_base = manifest_base.rsplit("/", 1)[0] + "/"

    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    download_start = time.time()

    with requests.Session() as download_session:
        download_session.headers.update(headers)

        first_template = selected_track["url_template"]
        if "{bitrate}" in first_template:
            first_template = first_template.replace("{bitrate}", str(selected_track["bitrate"]))
        if "{start time}" in first_template:
            first_template = first_template.replace("{start time}", str(chunk_times[0]))
        if "{start_time}" in first_template:
            first_template = first_template.replace("{start_time}", str(chunk_times[0]))
        first_url = manifest_base + first_template

        first_response = download_session.get(first_url, timeout=TIMEOUT)
        first_response.raise_for_status()
        first_segment = first_response.content

        with open(temp_path, "wb") as output_file:
            params = {
                "track_id": selected_track["track_id"],
                "duration": selected_track["duration"],
                "kid": None,
                "timescale": selected_track["timescale"],
                "language": selected_track["language"],
                "height": selected_track["height"],
                "width": selected_track["width"],
                "pssh": selected_track["pssh"],
                "media_type": selected_track["media_type"],
                "is_drm_protected": selected_track["is_drm_protected"],
                "codec": str(selected_track["codec"]).lower(),
                "codec_private_data": selected_track["codec_private_data"],
                "channels": selected_track["channels"],
                "bits": selected_track["bits"],
                "sample_rate": selected_track["sample_rate"],
                "nal_unit_length_field": selected_track["nal_unit_length_field"],
                "bitrate": selected_track["bitrate"],
                "first_segment": first_segment,
                "stream_name": selected_track["stream_name"],
            }
            parser_module.write_piff_header(output_file, params)

            output_file.write(first_segment)

            ratio = 1.0 if len(chunk_times) <= 0 else max(0.0, min(1.0, 1 / len(chunk_times)))
            filled = int(DOWNLOAD_PROGRESS_WIDTH * ratio)
            bar = "■" * filled + " " * (DOWNLOAD_PROGRESS_WIDTH - filled)
            elapsed = max(0.0, time.time() - download_start)
            remaining = 0.0 if ratio <= 0 else max(0.0, elapsed * (1.0 - ratio) / ratio)
            elapsed_s = int(round(elapsed))
            remaining_s = int(round(remaining))
            elapsed_h, elapsed_rem = divmod(elapsed_s, 3600)
            elapsed_m, elapsed_sec = divmod(elapsed_rem, 60)
            remaining_h, remaining_rem = divmod(remaining_s, 3600)
            remaining_m, remaining_sec = divmod(remaining_rem, 60)
            print(
                f"[{bar}] {ratio * 100:6.2f}% (elapsed: {elapsed_h:02d}:{elapsed_m:02d}:{elapsed_sec:02d}, remaining: {remaining_h:02d}:{remaining_m:02d}:{remaining_sec:02d})",
                end="\r" if len(chunk_times) > 1 else "\n",
                flush=True,
            )

            for index, chunk_start in enumerate(chunk_times[1:], start=2):
                template = selected_track["url_template"]
                if "{bitrate}" in template:
                    template = template.replace("{bitrate}", str(selected_track["bitrate"]))
                if "{start time}" in template:
                    template = template.replace("{start time}", str(chunk_start))
                if "{start_time}" in template:
                    template = template.replace("{start_time}", str(chunk_start))
                fragment_url = manifest_base + template

                with download_session.get(fragment_url, stream=True, timeout=TIMEOUT) as fragment_response:
                    fragment_response.raise_for_status()
                    for piece in fragment_response.iter_content(chunk_size=CHUNK_SIZE):
                        if piece:
                            output_file.write(piece)

                ratio = 1.0 if len(chunk_times) <= 0 else max(0.0, min(1.0, index / len(chunk_times)))
                filled = int(DOWNLOAD_PROGRESS_WIDTH * ratio)
                bar = "■" * filled + " " * (DOWNLOAD_PROGRESS_WIDTH - filled)
                elapsed = max(0.0, time.time() - download_start)
                remaining = 0.0 if ratio <= 0 else max(0.0, elapsed * (1.0 - ratio) / ratio)
                elapsed_s = int(round(elapsed))
                remaining_s = int(round(remaining))
                elapsed_h, elapsed_rem = divmod(elapsed_s, 3600)
                elapsed_m, elapsed_sec = divmod(elapsed_rem, 60)
                remaining_h, remaining_rem = divmod(remaining_s, 3600)
                remaining_m, remaining_sec = divmod(remaining_rem, 60)
                print(
                    f"[{bar}] {ratio * 100:6.2f}% (elapsed: {elapsed_h:02d}:{elapsed_m:02d}:{elapsed_sec:02d}, remaining: {remaining_h:02d}:{remaining_m:02d}:{remaining_sec:02d})",
                    end="\n" if index >= len(chunk_times) else "\r",
                    flush=True,
                )

    temp_path.replace(output_path)

    print("[+] Done")
    print(f"[+] Saved file: {output_path}")
    print(f"[+] Saved manifest copy: {manifest_copy_path}")

except KeyboardInterrupt:
    print("\n[!] Cancelled by user", file=sys.stderr)
    raise SystemExit(130)
except Exception as exc:
    print(f"[!] ERROR: {exc}", file=sys.stderr)
    raise SystemExit(1)