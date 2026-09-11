"""実LiveKitでbuffer有無と入力前RTP、独立Opus decodeを比較する診断。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from array import array
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import dotenv_values
from livekit import api, rtc

ROOT = Path(__file__).resolve().parents[2]


def calibrate_worker_clock(row: dict[str, Any]) -> None:
    """timeOrigin換算を採用せず、往復messageの因果関係からoffsetを囲む。"""
    clocks = row["clocks"]
    if len(clocks) != 10:
        raise ValueError("worker clock calibration incomplete")
    # このChromium診断では100us時計の差分に対し、両端合計200usの丸め余裕を残す。
    rounding_ms = 0.2
    lower = max(clock["lower"] - clock["workerNow"] for clock in clocks) - rounding_ms
    upper = min(clock["upper"] - clock["workerNow"] for clock in clocks) + rounding_ms
    row["clockQuantizationAllowanceMs"] = rounding_ms
    if not math.isfinite(lower + upper) or not 0 <= upper - lower <= 1:
        raise ValueError("worker clock calibration wider than 1ms or inconsistent")
    row["workerClockOffsetBounds"] = {"lowerMs": lower, "upperMs": upper}
    row["originOffsetDifferenceMs"] = (
        clocks[-1]["workerOrigin"] - row["mainTimeOrigin"] - lower
    )
    for packet in row["packets"]:
        packet["receivedAtBounds"] = {
            "lowerMs": lower + packet["rawReceiveTime"],
            "upperMs": upper + packet["rawReceiveTime"],
        }
    for frame in row["decoded"]:
        frame["decodedAtBounds"] = {
            "lowerMs": lower + frame["workerNow"],
            "upperMs": upper + frame["workerNow"],
        }


def verify_direct_chain(snapshots: list[dict[str, Any]]) -> dict[str, object]:
    """実packet→独立decode→出力の診断条件を検証する。受入cohortの代用ではない。"""
    for snapshot in snapshots:
        calibrate_worker_clock(snapshot)
    direct = {row["phase"]: row for row in snapshots if row["mode"] == "direct"}
    for row in snapshots:
        if row["errors"]:
            raise ValueError("media observation failed")
    for phase, expected in (
        ("before_input", 0),
        ("after_first_20ms", 1),
        ("after_paced_audio", 51),
    ):
        row = direct[phase]
        if any(
            len(row[kind]) != expected
            for kind in ("packets", "decoded", "rendered", "completed")
        ):
            raise ValueError("direct source frame count mismatch")
    row = direct["after_paced_audio"]
    packets, decoded, rendered, completed = (
        row[key] for key in ("packets", "decoded", "rendered", "completed")
    )
    # packetIndexは逐次decodeの入力単位。RTP clockやsource PCM offsetと見なさない。
    receive_decode = []
    decode_output = []
    if (
        packets[0]["receivedAtBounds"]["lowerMs"]
        < direct["before_input"]["outputObservation"]["observedAtMs"]
    ):
        raise ValueError("response packet arrived before input was supplied")
    origin = packets[0]["rtpTimestamp"]
    sequence = packets[0]["sequenceNumber"]
    for index, (packet, frame, output, end) in enumerate(
        zip(packets, decoded, rendered, completed, strict=True)
    ):
        # 次の入力を渡す前にoutput callbackの完了を待ち、途中flushしない。
        # decoderの再構成timestampをpacket識別子として使用しない。
        if packet["index"] != index or any(
            item["packetIndex"] != index for item in (frame, output, end)
        ):
            raise ValueError("packet decode output association mismatch")
        if (packet["rtpTimestamp"] - origin) % (2**32) != index * 960 or (
            packet["sequenceNumber"] - sequence
        ) % 65536 != index:
            raise ValueError("unexpected RTP discontinuity")
        if (
            frame["frames"] != 960
            or frame["sampleRate"] != 48000
            or frame["channels"] != 1
            or output["samples"] != 960
        ):
            raise ValueError("decoded frame format mismatch")
        if end["endFrame"] - output["firstFrame"] != 960:
            raise ValueError("decoded packet was not rendered contiguously")
        times = (packet["rawReceiveTime"], frame["workerNow"])
        output_delay_lower = output["outputAtMs"] - frame["decodedAtBounds"]["upperMs"]
        output_delay_upper = output["outputAtMs"] - frame["decodedAtBounds"]["lowerMs"]
        if (
            not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
                for value in times
            )
            or not times[0] <= times[1]
            or not 0 <= output_delay_lower <= output_delay_upper
        ):
            raise ValueError("receive decode output clock ordering failed")
        receive_decode.append(times[1] - times[0])
        decode_output.append(
            {"lowerMs": output_delay_lower, "upperMs": output_delay_upper}
        )
    observation = row["outputObservation"]
    if (
        observation["outputTimestamp"]["contextTime"] * observation["sampleRate"]
        < completed[-1]["endFrame"]
    ):
        raise ValueError("output clock has not passed the rendered samples")
    return {
        "status": "verified_diagnostic_only",
        "packets": len(packets),
        "decoded_samples": sum(frame["frames"] for frame in decoded),
        "rendered_packets": len(rendered),
        "max_decoder_timestamp_deviation_us": max(
            abs(frame["chunkTimestamp"] - index * 20000)
            for index, frame in enumerate(decoded)
        ),
        "completed_packets": len(completed),
        "pre_input_packets": 0,
        "first_receive_to_decode_ms": receive_decode[0],
        "first_decode_to_output_bounds_ms": decode_output[0],
        "max_receive_to_decode_ms": max(receive_decode),
        "max_decode_to_output_upper_ms": max(
            value["upperMs"] for value in decode_output
        ),
        "worker_clock_offset_bounds_ms": row["workerClockOffsetBounds"],
        "origin_offset_difference_ms": row["originOffsetDifferenceMs"],
        "output_clock_passed_all_samples": True,
        "source_pcm_offset_verified": False,
        "production_playback_verified": False,
    }


def verify_decoder_comparison(row: dict[str, Any]) -> None:
    """同じsnapshotの全PCMと一括復号の一致を要求する。flush比較は差分を報告する。"""
    comparison = row["decoder_comparison"]
    if (
        comparison.get("kind") != "comparison"
        or comparison.get("perPacketFlush") is not False
    ):
        raise ValueError("decoder comparison unavailable")
    expected_packets = len(row["packets"])
    expected_samples = sum(frame["frames"] for frame in row["decoded"])
    if not expected_packets or len(row["decoded"]) != expected_packets:
        raise ValueError("decoder comparison frame count unavailable")
    for method in ("serialVersusBatch", "flushedVersusBatch"):
        result = comparison[method]
        if (
            result["packets"] != expected_packets
            or result["samples"] != expected_samples
        ):
            raise ValueError("decoder comparison input count mismatch")
        for key in ("maxAbsoluteError", "rmsError"):
            value = result[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("invalid decoder comparison error")
            if method == "serialVersusBatch" and value != 0:
                raise ValueError("serial decode differs from continuous reference")


async def run(output: Path) -> None:
    # 失敗した診断も上書きしない。秘密値はstdinだけで子processへ渡す。
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as report_file:
        keys = dotenv_values(ROOT / "infra/livekit/.env").get("LIVEKIT_KEYS")
        if not keys or ":" not in keys:
            raise ValueError("test LiveKit keys are unavailable")
        key, secret = (value.strip() for value in keys.split(":", 1))
        room_name = "voice-quality-source-" + str(uuid4())

        def token(identity: str) -> str:
            return (
                api.AccessToken(key, secret)
                .with_identity(identity)
                .with_grants(
                    api.VideoGrants(room_join=True, room=room_name),
                )
                .to_jwt()
            )

        sender = rtc.Room()
        child = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "frontend/scripts/probe-native-audio-source.mjs"),
            cwd=ROOT / "frontend",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=2 * 1024 * 1024,
        )
        assert child.stdin is not None and child.stdout is not None

        async def send(value: dict[str, object]) -> None:
            child.stdin.write((json.dumps(value) + "\n").encode())
            await child.stdin.drain()

        async def receive(kind: str) -> dict[str, Any]:
            while True:
                line = await asyncio.wait_for(child.stdout.readline(), 30)
                if not line:
                    raise RuntimeError("browser probe ended before its observation")
                row = json.loads(line)
                if row.get("kind") == "stage":
                    result["last_browser_stage"] = row["stage"]
                if row.get("kind") == "failed":
                    raise RuntimeError("browser probe failed")
                if row.get("kind") == kind:
                    return row

        sources = []
        snapshots = []
        result: dict[str, object] = {
            "scope": "native_source_and_independent_opus_decode_diagnostic",
            "python_livekit_version": version("livekit"),
            "observations": snapshots,
        }
        try:
            await send(
                {"url": "ws://127.0.0.1:7880", "token": token("source-observer")}
            )
            result["browser"] = (await receive("ready"))["browser"]
            await sender.connect("ws://127.0.0.1:7880", token("source-publisher"))
            for mode, queue_size in (("buffered", 1000), ("direct", 0)):
                source = rtc.AudioSource(48000, 1, queue_size_ms=queue_size)
                sources.append(source)
                track = rtc.LocalAudioTrack.create_audio_track(mode, source)
                publication = await sender.local_participant.publish_track(track)
                await receive("subscribed")
                await asyncio.sleep(1)

                async def snapshot(phase: str, mode: str = mode) -> None:
                    await send({"kind": "snapshot"})
                    row = (await receive("snapshot"))["rows"][mode]
                    snapshots.append({"mode": mode, "phase": phase, **row})

                await snapshot("before_input")
                # 最初の20msだけ送り、その後200ms停止する。入力とRTPの対応を調べる。
                for frame_index in range(2):
                    pcm = array(
                        "h",
                        (
                            round(
                                6000
                                * math.sin(
                                    2 * math.pi * 440 * (frame_index * 480 + i) / 48000
                                )
                            )
                            for i in range(480)
                        ),
                    )
                    await asyncio.wait_for(
                        source.capture_frame(
                            rtc.AudioFrame(pcm.tobytes(), 48000, 1, 480)
                        ),
                        5,
                    )
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.2)
                await snapshot("after_first_20ms")
                deadline = asyncio.get_running_loop().time()
                for frame_index in range(2, 102):
                    pcm = array(
                        "h",
                        (
                            round(
                                6000
                                * math.sin(
                                    2 * math.pi * 440 * (frame_index * 480 + i) / 48000
                                )
                            )
                            for i in range(480)
                        ),
                    )
                    await asyncio.wait_for(
                        source.capture_frame(
                            rtc.AudioFrame(pcm.tobytes(), 48000, 1, 480)
                        ),
                        5,
                    )
                    deadline += 0.01
                    await asyncio.sleep(
                        max(0, deadline - asyncio.get_running_loop().time())
                    )
                await asyncio.sleep(0.5)
                await snapshot("after_paced_audio")
                await sender.local_participant.unpublish_track(publication.sid)
                await source.aclose()
                sources.remove(source)
                # publisher停止後に同じencoded packetを一括decodeしてPCMを比較する。
                await asyncio.sleep(0.2)
                await send(
                    {
                        "kind": "compare",
                        "mode": mode,
                        "count": len(snapshots[-1]["packets"]),
                    }
                )
                comparison = (await receive("comparison"))["result"]
                snapshots[-1]["decoder_comparison"] = comparison
            await send({"kind": "close"})
            await receive("closed")
            child.stdin.close()
            await asyncio.wait_for(child.wait(), 15)
            if child.returncode:
                raise RuntimeError("browser probe failed during cleanup")
            result["verification"] = verify_direct_chain(snapshots)
            for row in snapshots:
                if row["phase"] == "after_paced_audio":
                    verify_decoder_comparison(row)
                    if row["mode"] == "direct":
                        result["decoder_comparison"] = row["decoder_comparison"]
            result["outcome"] = "observed"
        except BaseException as error:
            result["outcome"] = "failed"
            result["failure_type"] = type(error).__name__
            raise
        finally:
            for source in sources:
                await source.aclose()
            await sender.disconnect()
            if child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), 5)
                except TimeoutError:
                    child.kill()
                    await child.wait()
            async with api.LiveKitAPI("http://127.0.0.1:7880", key, secret) as client:
                await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            result["cleanup_confirmed"] = True
            report_file.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
        print(
            json.dumps(
                {
                    "output": str(output),
                    "verification": result.get("verification"),
                    "observations": [
                        {
                            "mode": row["mode"],
                            "phase": row["phase"],
                            "packets": len(row["packets"]),
                            "decoded_frames": len(row["decoded"]),
                            "errors": row["errors"],
                        }
                        for row in snapshots
                    ],
                }
            )
        )


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments.output.resolve()))
    except Exception as error:  # noqa: BLE001 -- 秘密値を含み得る例外本文を出力しない。
        print(json.dumps({"probe_failed_type": type(error).__name__}))
        raise SystemExit(1) from None
