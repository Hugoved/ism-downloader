import io
import time
import base64
import binascii
import re
import inspect
import bitstring
from struct import Struct

u8 = Struct(">B")
u88 = Struct(">Bx")
u16 = Struct(">H")
u1616 = Struct(">Hxx")
u32 = Struct(">I")
u64 = Struct(">Q")

s88 = Struct(">bx")
s16 = Struct(">h")
s1616 = Struct(">hxx")
s32 = Struct(">i")

unity_matrix = (s32.pack(0x10000) + s32.pack(0) * 3) * 2 + s32.pack(0x40000000)

TRACK_ENABLED = 0x1
TRACK_IN_MOVIE = 0x2
TRACK_IN_PREVIEW = 0x4

SELF_CONTAINED = 0x1
PLAYREADY_SYSTEM_ID = "9a04f07998404286ab92e65be0885f95"
WIDEVINE_SYSTEM_ID = "edef8ba979d64acea3c827dcd51d21ed"
START_CODE = b"\x00\x00\x00\x01"


def to_rbsp(oldStr):
    newStr = oldStr

    while True:
        iter_matches = re.finditer("00000300|00000301|00000302", oldStr)
        ll = [m.start(0) for m in iter_matches]
        if ll == []:
            break

        indices = [x for x in ll if x & 1 == 0]
        if indices == []:
            break

        s = 0
        for k in indices:
            tmpStr = newStr[0 : k - s] + oldStr[k:].replace("000003", "0000", 1)
            s += 2
            newStr = tmpStr

        oldStr = newStr

    return newStr


def read_past_scaling_matrix(s, matrix_size):
    next_scale = 8
    last_scale = 8

    for _ in range(matrix_size):
        if next_scale:
            delta_scale = s.read("se")
            next_scale = (last_scale + delta_scale + 256) & 0xFF
        if next_scale:
            last_scale = next_scale


def parse_avc_sps(data) -> dict:
    spsLen = len(data)
    ret = {}

    c = bitstring.BitArray(bytes=data, length=spsLen * 8)
    newStr = to_rbsp(c.hex)
    s = bitstring.BitStream("0x" + newStr)

    s.read("uint:16")
    ret["profile"] = s.read("uint:8")
    ret["profile_compatibility"] = s.read("uint:8")
    ret["level"] = s.read("uint:8")
    ret["parameter_id"] = s.read("ue")
    ret["chroma_format_idc"] = 1
    if ret["profile"] == 138:
        ret["chroma_format_idc"] = 0

    if ret["profile"] in [100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135]:
        ret["chroma_format_idc"] = s.read("ue")
        if ret["chroma_format_idc"] == 3:
            ret["separate_colour_plane_flag"] = s.read("uint:1")

        ret["bit_depth_luma"] = s.read("ue") + 8
        ret["bit_depth_chroma"] = s.read("ue") + 8
        ret["qp_prime_y_zero_transform_bypass_flag"] = s.read("uint:1")
        ret["seq_scaling_matrix_present_flag"] = s.read("uint:1")
        if ret["seq_scaling_matrix_present_flag"]:
            for k in range(8):
                seq_scaling_list_present_flag = s.read("uint:1")
                if seq_scaling_list_present_flag:
                    if k < 6:
                        read_past_scaling_matrix(s, 16)
                    else:
                        read_past_scaling_matrix(s, 64)

    ret["log2_max_frame_num"] = s.read("ue") + 4
    ret["pic_order_cnt_type"] = s.read("ue")
    if ret["pic_order_cnt_type"] == 0:
        ret["log2_max_pic_order_cnt_lsb"] = s.read("ue") + 4
    elif ret["pic_order_cnt_type"] == 1:
        ret["delta_pic_order_always_zero_flag"] = s.read("uint:1")
        ret["offset_for_non_ref_pic"] = s.read("ue")
        ret["offset_for_top_to_bottom_field"] = s.read("ue")
        num_ref_frames_in_pic_order_cnt_cycle = s.read("ue")
        ret["ref_frames_in_pic_order_cnt_cycle"] = []
        for _ in range(num_ref_frames_in_pic_order_cnt_cycle):
            ret["ref_frames_in_pic_order_cnt_cycle"].append(s.read("ue"))

    ret["num_ref_frames"] = s.read("ue")
    ret["gaps_in_frame_num_value_allowed_flag"] = s.read("uint:1")
    ret["width"] = (s.read("ue") + 1) * 16
    ret["height"] = (s.read("ue") + 1) * 16

    ret["frame_mbs_only_flag"] = frame_mbs_only = s.read("uint:1")
    if not ret["frame_mbs_only_flag"]:
        ret["mb_adaptive_frame_field_flag"] = s.read("uint:1")
        ret["height"] *= 2

    ret["direct_8x8_inference_flag"] = s.read("uint:1")
    ret["frame_cropping_flag"] = s.read("uint:1")

    if ret["frame_cropping_flag"]:
        if ret["chroma_format_idc"] == 0:
            crop_unit_x, crop_unit_y = 1, 2 - frame_mbs_only
        elif ret["chroma_format_idc"] == 1:
            crop_unit_x, crop_unit_y = 2, 2 * (2 - frame_mbs_only)
        elif ret["chroma_format_idc"] == 2:
            crop_unit_x, crop_unit_y = 2, 1 * (2 - frame_mbs_only)
        elif ret["chroma_format_idc"] == 3:
            crop_unit_x, crop_unit_y = 1, 1 * (2 - frame_mbs_only)
        else:
            raise ValueError("invalid chroma format idc")

        ret["frame_crop_left_offset"] = s.read("ue")
        ret["frame_crop_right_offset"] = s.read("ue")
        ret["frame_crop_top_offset"] = s.read("ue")
        ret["frame_crop_bottom_offset"] = s.read("ue")

        frame_crop_width = ret["frame_crop_left_offset"] + ret["frame_crop_right_offset"]
        frame_crop_height = ret["frame_crop_top_offset"] + ret["frame_crop_bottom_offset"]
        ret["width"] -= frame_crop_width * crop_unit_x
        ret["height"] -= frame_crop_height * crop_unit_y

    ret["vui_parameters_present_flag"] = s.read("uint:1")
    if ret["vui_parameters_present_flag"]:
        ret["aspect_ratio_info_present_flag"] = s.read("uint:1")
        if ret["aspect_ratio_info_present_flag"]:
            ret["aspect_ratio_idc"] = s.read("uint:8")
            if ret["aspect_ratio_idc"] == 255:
                ret["sar_width"] = s.read("uint:16")
                ret["sar_height"] = s.read("uint:16")
        ret["overscan_info_present_flag"] = s.read("uint:1")
        if ret["overscan_info_present_flag"]:
            ret["overscan_appropriate_flag"] = s.read("uint:1")
        ret["video_signal_type_present_flag"] = s.read("uint:1")
        if ret["video_signal_type_present_flag"]:
            ret["video_format"] = s.read("uint:3")
            ret["video_full_range_flag"] = s.read("uint:1")
            ret["colour_description_present_flag"] = s.read("uint:1")
            if ret["colour_description_present_flag"]:
                ret["colour_primaries"] = s.read("uint:8")
                ret["transfer_characteristics"] = s.read("uint:8")
                ret["matrix_coefficients"] = s.read("uint:8")
        ret["chroma_loc_info_present_flag"] = s.read("uint:1")
        if ret["chroma_loc_info_present_flag"]:
            ret["chroma_sample_loc_type_top_field"] = s.read("ue")
            ret["chroma_sample_loc_type_bottom_field"] = s.read("ue")
        ret["vui_timing_info_present_flag"] = s.read("uint:1")
        if ret["vui_timing_info_present_flag"]:
            ret["vui_num_units_in_tick"] = s.read("uint:32")
            ret["vui_time_scale"] = s.read("uint:32")
            ret["vui_fixed_framerate_flag"] = s.read("uint:1")
    return ret


def parse_hevc_sps(data) -> dict:
    spsLen = len(data)
    ret = {}

    c = bitstring.BitArray(bytes=data, length=spsLen * 8)
    newStr = to_rbsp(c.hex)
    s = bitstring.BitStream("0x" + newStr)

    s.read("uint:16")
    ret["sps_video_parameter_set_id"] = s.read("uint:4")
    ret["sps_max_sub_layers"] = s.read("uint:3") + 1
    ret["sps_temporal_id_nesting_flag"] = s.read("uint:1")
    ret["general_profile_space"] = s.read("uint:2")
    ret["general_tier_flag"] = s.read("uint:1")
    ret["general_profile_idc"] = s.read("uint:5")
    ret["general_compatibility_flags"] = s.read("uint:32")
    ret["general_progressive_source_flag"] = s.read("uint:1")
    ret["general_interlaced_source_flag"] = s.read("uint:1")
    ret["general_non_packed_constraint_flag"] = s.read("uint:1")
    ret["general_frame_only_constraint_flag"] = s.read("uint:1")
    s.read("uint:32")
    s.read("uint:12")
    ret["general_level_idc"] = s.read("uint:8")

    if ret["sps_max_sub_layers"] >= 2:
        max_num_sub_layers = ret["sps_max_sub_layers"]
        for _ in range(max_num_sub_layers - 1):
            s.read("uint:1")
            s.read("uint:1")
        k = max_num_sub_layers - 1
        while k < 8:
            s.read("uint:2")
            k += 1

    ret["sps_seq_parameter_set_id"] = s.read("ue")
    ret["chroma_format_idc"] = s.read("ue")
    if ret["chroma_format_idc"] == 3:
        ret["separate_colour_plane_flag"] = s.read("uint:1")
    ret["pic_width_in_luma_samples"] = s.read("ue")
    ret["pic_height_in_luma_samples"] = s.read("ue")
    ret["conformance_window_flag"] = s.read("uint:1")
    if ret["conformance_window_flag"]:
        ret["conf_win_left_offset"] = s.read("ue")
        ret["conf_win_right_offset"] = s.read("ue")
        ret["conf_win_top_offset"] = s.read("ue")
        ret["conf_win_bottom_offset"] = s.read("ue")
    ret["bit_depth_luma"] = s.read("ue") + 8
    ret["bit_depth_chroma"] = s.read("ue") + 8
    ret["log2_max_pic_order_cnt_lsb"] = s.read("ue") + 4
    ret["sps_sub_layer_ordering_info"] = s.read("uint:1")

    if ret["sps_sub_layer_ordering_info"]:
        k = 0
    else:
        k = ret["sps_max_sub_layers"] - 1
    while k <= ret["sps_max_sub_layers"] - 1:
        s.read("ue")
        s.read("ue")
        s.read("ue")
        k += 1

    ret["log2_min_coding_block_size"] = s.read("ue") + 3
    ret["log2_diff_max_min_coding_block_size"] = s.read("ue")
    ret["log2_min_transform_block_size"] = s.read("ue") + 2
    ret["log2_diff_max_min_transform_block_size"] = s.read("ue")
    ret["max_transform_hierarchy_depth_inter"] = s.read("ue")
    ret["max_transform_hierarchy_depth_intra"] = s.read("ue")
    ret["scaling_list_enabled_flag"] = s.read("uint:1")
    assert ret["scaling_list_enabled_flag"] == 0
    ret["amp_enabled_flag"] = s.read("uint:1")
    ret["sample_adaptive_offset_enabled_flag"] = s.read("uint:1")
    ret["pcm_enabled_flag"] = s.read("uint:1")
    if ret["pcm_enabled_flag"]:
        s.read("uint:4")
        s.read("uint:4")
        s.read("ue")
        s.read("ue")
        s.read("uint:1")

    ret["num_short_term_ref_pic_sets"] = s.read("ue")
    numRefs = 0
    if ret["num_short_term_ref_pic_sets"] > 0:
        for k in range(ret["num_short_term_ref_pic_sets"]):
            inter_ref_pic_set_prediction_flag = 0
            if k > 0:
                inter_ref_pic_set_prediction_flag = s.read("uint:1")
            if inter_ref_pic_set_prediction_flag:
                refFrames = 0
                for _ in range(numRefs + 1):
                    used_by_curr_pic_flag = s.read("uint:1")
                    if used_by_curr_pic_flag != 0:
                        refFrames += 1
                numRefs = refFrames
            else:
                num_negative_pics = s.read("ue")
                num_positive_pics = s.read("ue")
                numRefs = num_negative_pics + num_positive_pics
                for _ in range(num_negative_pics):
                    s.read("ue")
                    s.read("uint:1")
                for _ in range(num_positive_pics):
                    s.read("ue")
                    s.read("uint:1")

    ret["long_term_ref_pics_present_flag"] = s.read("uint:1")
    if ret["long_term_ref_pics_present_flag"]:
        num_long_term_ref_pics = s.read("ue")
        for _ in range(num_long_term_ref_pics):
            s.read(f"uint:{ret['log2_max_pic_order_cnt_lsb']}")
            s.read("uint:1")

    ret["sps_temporal_mvp_enable_flag"] = s.read("uint:1")
    ret["sps_strong_intra_smoothing_enable_flag"] = s.read("uint:1")
    ret["vui_parameters_present_flag"] = s.read("uint:1")
    if ret["vui_parameters_present_flag"]:
        ret["aspect_ratio_info_present_flag"] = s.read("uint:1")
        if ret["aspect_ratio_info_present_flag"]:
            ret["aspect_ratio_idc"] = s.read("uint:8")
            if ret["aspect_ratio_idc"] == 255:
                ret["sar_width"] = s.read("uint:16")
                ret["sar_height"] = s.read("uint:16")
        ret["overscan_info_present_flag"] = s.read("uint:1")
        if ret["overscan_info_present_flag"]:
            ret["overscan_appropriate_flag"] = s.read("uint:1")
        ret["video_signal_type_present_flag"] = s.read("uint:1")
        if ret["video_signal_type_present_flag"]:
            ret["video_format"] = s.read("uint:3")
            ret["video_full_range_flag"] = s.read("uint:1")
            ret["colour_description_present_flag"] = s.read("uint:1")
            if ret["colour_description_present_flag"]:
                ret["colour_primaries"] = s.read("uint:8")
                ret["transfer_characteristics"] = s.read("uint:8")
                ret["matrix_coefficients"] = s.read("uint:8")
        ret["chroma_loc_info_present_flag"] = s.read("uint:1")
        if ret["chroma_loc_info_present_flag"]:
            ret["chroma_sample_loc_type_top_field"] = s.read("ue")
            ret["chroma_sample_loc_type_bottom_field"] = s.read("ue")
        ret["neutral_chroma_indication_flag"] = s.read("uint:1")
        ret["field_seq_flag"] = s.read("uint:1")
        ret["frame_field_info_present_flag"] = s.read("uint:1")
        ret["default_display_window_flag"] = s.read("uint:1")
        if ret["default_display_window_flag"]:
            ret["def_disp_win_left_offset"] = s.read("ue")
            ret["def_disp_win_right_offset"] = s.read("ue")
            ret["def_disp_win_top_offset"] = s.read("ue")
            ret["def_disp_win_bottom_offset"] = s.read("ue")
        ret["vui_timing_info_present_flag"] = s.read("uint:1")
        if ret["vui_timing_info_present_flag"]:
            ret["vui_num_units_in_tick"] = s.read("uint:32")
            ret["vui_time_scale"] = s.read("uint:32")
            ret["vui_poc_proportional_to_timing_flag"] = s.read("uint:1")
            if ret["vui_poc_proportional_to_timing_flag"]:
                ret["vui_num_ticks_poc_diff_one"] = s.read("ue") + 1
        ret["hrd_parameters_present_flag"] = s.read("uint:1")
    ret["sps_extension_flag"] = s.read("uint:1")
    return ret


def get_real_fps_from_codec_private_data(codec, codec_private_data):
    if not codec_private_data:
        return None
    try:
        if isinstance(codec_private_data, bytes):
            blob = bytes(codec_private_data)
        else:
            cleaned = str(codec_private_data).strip().replace(" ", "").replace("-", "")
            if not cleaned:
                return None
            blob = binascii.unhexlify(cleaned.encode())
        arr = blob.split(u32.pack(1))
        codec_lower = str(codec or "").lower()
        if codec_lower == "avc1":
            sps = next((x for x in arr if x and (x[0] & 0x1F) == 7), None)
            if not sps:
                return None
            sps_data = parse_avc_sps(sps)
            if "vui_num_units_in_tick" in sps_data and "vui_time_scale" in sps_data and sps_data["vui_num_units_in_tick"]:
                return sps_data["vui_time_scale"] / sps_data["vui_num_units_in_tick"] / 2
            return None
        if codec_lower in ("hvc1", "hev1", "dvhe", "dvh1"):
            sps = next((x for x in arr if x and ((x[0] >> 1) == 0x21)), None)
            if not sps:
                return None
            sps_data = parse_hevc_sps(sps)
            if "vui_num_units_in_tick" in sps_data and "vui_time_scale" in sps_data and sps_data["vui_num_units_in_tick"]:
                return sps_data["vui_time_scale"] / sps_data["vui_num_units_in_tick"]
    except Exception:
        return None
    return None


class BinaryWriter:
    def __init__(self, stream):
        self.stream = stream

    def WriteUInt(self, n, offset=0):
        if isinstance(n, str):
            n = n.encode("ascii")

        n = u32.pack(n)
        if offset:
            n = n[offset:]

        return self.stream.write(n)

    def Write(self, n):
        if isinstance(n, str):
            n = n.encode("ascii")

        if not isinstance(n, int):
            return self.stream.write(bytes(n))

        n = u8.pack(n)

        return self.stream.write(n)

    def WriteInt(self, n):
        if isinstance(n, str):
            n = n.encode("ascii")

        n = s32.pack(n)

        return self.stream.write(n)

    def WriteULong(self, n):
        if isinstance(n, str):
            n = n.encode("ascii")

        n = u64.pack(n)

        return self.stream.write(n)

    def WriteUShort(self, n, padding=0):
        if isinstance(n, str):
            n = n.encode("ascii")

        n = (u16 if not padding else u1616).pack(n)

        return self.stream.write(n)

    def WriteShort(self, n, padding=0):
        if isinstance(n, str):
            n = n.encode("ascii")

        n = (s16 if not padding else s1616).pack(n)

        return self.stream.write(n)

    def WriteByte(self, n, padding=0):
        if isinstance(n, str):
            n = n.encode("ascii")

        if not isinstance(n, int):
            return self.stream.write(bytes(n))

        n = (u8 if not padding else s88).pack(n)

        return self.stream.write(n)


def write_piff_header(stream, params):
    track_id = int(params.get("track_id", 1) or 1)
    duration = int(params["duration"])
    kid = params.get("kid")
    timescale = int(params.get("timescale", 10000000) or 10000000)
    language = str(params.get("language", "und") or "und").lower()
    height = int(params.get("height", 0) or 0)
    width = int(params.get("width", 0) or 0)
    pssh = params.get("pssh")
    type = params["media_type"]
    is_drm_protected = bool(params.get("is_drm_protected"))

    if len(language) != 3:
        language = "und"

    first_segment = params.get("first_segment") or params.get("init_segment")
    if first_segment:
        try:
            blob = first_segment if isinstance(first_segment, (bytes, bytearray)) else bytes(first_segment)
            moof_data = extract_box_data(blob, [b"moof"])
            traf_data = extract_box_data(moof_data, [b"traf"])
            tfhd_data = extract_box_data(traf_data, [b"tfhd"])
            if tfhd_data and len(tfhd_data) >= 8:
                track_id = int.from_bytes(tfhd_data[4:8], byteorder="big")
        except Exception:
            pass

    if is_drm_protected:
        normalized_kid = None

        if kid is not None:
            if isinstance(kid, bytes):
                if len(kid) == 16:
                    normalized_kid = kid.hex()
                else:
                    try:
                        normalized_kid = kid.decode("ascii", errors="ignore")
                    except Exception:
                        normalized_kid = kid.hex()
            else:
                normalized_kid = str(kid).strip()

            normalized_kid = normalized_kid.replace("-", "").replace(" ", "")
            if normalized_kid.startswith("0x"):
                normalized_kid = normalized_kid[2:]
            try:
                normalized_kid = bytes.fromhex(normalized_kid).hex()
            except Exception:
                normalized_kid = None

        if normalized_kid is None and pssh:
            protection_data = None
            if isinstance(pssh, bytes):
                protection_data = bytes(pssh)
            else:
                text = str(pssh).strip()
                try:
                    protection_data = base64.b64decode(text)
                except Exception:
                    try:
                        protection_data = bytes.fromhex(text.replace("-", "").replace(" ", ""))
                    except Exception:
                        protection_data = None

            if protection_data:
                try:
                    xml_text = protection_data.decode("utf-16-le", errors="ignore")
                    if "<KID>" not in xml_text.upper():
                        xml_text = protection_data.decode("utf-8", errors="ignore")
                    match = re.search(r"<KID>(.*?)</KID>", xml_text, flags=re.IGNORECASE | re.DOTALL)
                    if match:
                        kid_bytes = base64.b64decode(match.group(1))
                        if len(kid_bytes) == 16:
                            kid_bytes = bytearray(kid_bytes)
                            kid_bytes[0:4] = reversed(kid_bytes[0:4])
                            kid_bytes[4:6] = reversed(kid_bytes[4:6])
                            kid_bytes[6:8] = reversed(kid_bytes[6:8])
                            normalized_kid = bytes(kid_bytes).hex()
                except Exception:
                    pass

        kid = normalized_kid or ("00" * 16)
    else:
        kid = None

    file_type_box = get_file_type_box()
    stream.write(file_type_box)

    moov_payload = get_mvhd_box(timescale, duration)
    trak_payload = get_tkhd_box(track_id, duration, width, height)
    mdhd_payload = get_mdhd_box(language, timescale, duration)
    hdlr_payload = get_hdlr_box(type)

    mdia_payload = mdhd_payload + hdlr_payload
    minf_payload = get_minf_box(type)

    stbl_payload = full_box("stts", 0, 0, entry_count(0))
    stbl_payload += full_box("stsc", 0, 0, entry_count(0))
    stbl_payload += full_box("stco", 0, 0, entry_count(0))
    stbl_payload += full_box("stsz", 0, 0, entry_count(0) + entry_count(0))

    stsd_params = dict(params)
    stsd_params["track_id"] = track_id
    stsd_params["kid"] = kid
    stsd_payload = get_stsd_box(stsd_params, kid)
    stbl_payload += full_box("stsd", 0, 0, stsd_payload)

    stbl_box = box("stbl", stbl_payload)
    minf_payload += stbl_box

    minf_box = box("minf", minf_payload)
    mdia_payload += minf_box

    mdia_box = box("mdia", mdia_payload)
    trak_payload += mdia_box

    trak_box = box("trak", trak_payload)
    moov_payload += trak_box

    mvex_payload = get_mehd_box(duration)
    mvex_payload += get_trex_box(track_id)
    moov_payload += box("mvex", mvex_payload)

    if is_drm_protected:
        if pssh:
            moov_payload += get_playready_pssh_box(pssh)
        if kid:
            moov_payload += get_widevine_pssh_box(kid)

    stream.write(box("moov", moov_payload))
    return stream


def entry_count(byte):
    return u32.pack(byte)


def get_file_type_box():
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.Write(b"mp41")
    writer.WriteUInt(1)
    for compatible_brand in ("iso8","isom","mp41","dash","cmfc"):
        writer.Write(compatible_brand.encode("ascii"))

    return box("ftyp", stream.getvalue())


def get_mvhd_box(timescale, duration):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    now = int(time.time())
    writer.WriteULong(now)
    writer.WriteULong(now)
    writer.WriteUInt(timescale)
    writer.WriteULong(duration)
    writer.WriteUShort(1, padding=2)
    writer.WriteByte(1, padding=1)
    writer.WriteUShort(0)

    for _ in range(2):
        writer.WriteUInt(0)

    writer.Write(unity_matrix)

    for _ in range(6):
        writer.WriteUInt(0)

    writer.WriteUInt(0xFFFFFFFF)

    return full_box("mvhd", 1, 0, stream.getvalue())


def get_tkhd_box(track_id, duration, width, height):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    now = int(time.time())
    writer.WriteULong(now)
    writer.WriteULong(now)
    writer.WriteUInt(track_id)
    writer.WriteUInt(0)
    writer.WriteULong(duration)

    for _ in range(2):
        writer.WriteUInt(0)

    writer.WriteShort(0)
    writer.WriteShort(0)
    writer.WriteByte(1 if width == 0 and height == 0 else 0, padding=1)
    writer.WriteUShort(0)

    writer.Write(unity_matrix)

    writer.WriteUShort(width, padding=2)
    writer.WriteUShort(height, padding=2)

    return full_box(
        "tkhd",
        1,
        TRACK_ENABLED | TRACK_IN_MOVIE | TRACK_IN_PREVIEW,
        stream.getvalue(),
    )


def get_mdhd_box(language, timescale, duration):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    language = str(language or "und").lower()
    if len(language) != 3:
        language = "und"

    now = int(time.time())
    writer.WriteULong(now)
    writer.WriteULong(now)
    writer.WriteUInt(timescale)
    writer.WriteULong(duration)
    writer.WriteUShort(
        ((ord(language[0]) - 0x60) << 10)
        | ((ord(language[1]) - 0x60) << 5)
        | (ord(language[2]) - 0x60)
    )
    writer.WriteUShort(0)

    return full_box("mdhd", 1, 0, stream.getvalue())


def get_hdlr_box(type):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteUInt(0)
    if type == "audio":
        writer.Write(b"soun")

        for _ in range(3):
            writer.WriteUInt(0)

        writer.Write(b"audio\0")
    elif type == "video":
        writer.Write(b"vide")

        for _ in range(3):
            writer.WriteUInt(0)

        writer.Write(b"video\0")
    elif type == "text":
        writer.Write(b"subt")

        for _ in range(3):
            writer.WriteUInt(0)

        writer.Write(b"subtitle\0")
    else:
        raise NotImplementedError(f"Track Type {type!r} is not supported.")

    return full_box("hdlr", 0, 0, stream.getvalue())


def get_minf_box(type):
    if type == "audio":
        smhd_box = s88.pack(0)
        smhd_box += u16.pack(0)

        minf_payload = full_box("smhd", 0, 0, bytes(smhd_box))
    elif type == "video":
        vmhd_box = u16.pack(0)

        for _ in range(3):
            vmhd_box += u16.pack(0)

        minf_payload = full_box("vmhd", 0, 1, bytes(vmhd_box))
    elif type == "text":
        minf_payload = full_box("sthd", 0, 0, b"")
    else:
        raise NotImplementedError(f"Track Type {type!r} is not supported.")

    dref_payload = entry_count(1)
    dref_payload += full_box("url ", 0, SELF_CONTAINED, b"")

    dinf_payload = full_box("dref", 0, 0, bytes(dref_payload))
    minf_payload += box("dinf", bytes(dinf_payload))

    return bytes(minf_payload)


def get_stsd_box(params, kid):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteUInt(1)
    sample_entry_data = get_sample_entry_box(params, kid)
    writer.Write(sample_entry_data)

    return stream.getvalue()


def get_sample_entry_box(params, kid):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    for _ in range(6):
        writer.WriteByte(0)

    writer.WriteUShort(1)

    codec = str(params["codec"])
    codec_lower = codec.lower()
    type = params["media_type"]
    cpd = params.get("codec_private_data", "")

    if isinstance(cpd, bytes):
        codec_private_data = bytes(cpd)
    else:
        cleaned_cpd = str(cpd or "").strip().replace(" ", "").replace("-", "")
        codec_private_data = binascii.unhexlify(cleaned_cpd.encode()) if cleaned_cpd else b""

    if type == "audio":
        for _ in range(2):
            writer.WriteUInt(0)

        writer.WriteUShort(int(params.get("channels", 2) or 2))
        writer.WriteUShort(int(params.get("bits", 16) or 16))
        writer.WriteUShort(0)
        writer.WriteUShort(0)
        writer.WriteUShort(int(params.get("sample_rate", 48000) or 48000), padding=2)

        if "aac" in codec_lower:
            if codec_private_data:
                esds_box = get_esds_box(
                    int(params.get("track_id", 1) or 1),
                    int(params.get("bitrate", 0) or 0),
                    codec_private_data,
                )
                writer.Write(esds_box)

            if params.get("is_drm_protected"):
                sinf_box = get_sinf_box(kid, "mp4a")
                writer.Write(sinf_box)
                return box("enca", stream.getvalue())
            else:
                return box("mp4a", stream.getvalue())
        elif codec_lower in ("e-ac3", "ec-3", "ec3"):
            dec3_payload = params.get("dec3_payload") or params.get("dec3")
            if dec3_payload is not None:
                if isinstance(dec3_payload, str):
                    dec3_payload = bytes.fromhex(dec3_payload.replace(" ", "").replace("-", ""))
                writer.Write(full_box("dec3", 0, 0, bytes(dec3_payload)))
            else:
                atmos = bool(
                    params.get("is_atmos")
                    or params.get("joc")
                    or params.get("dolby_atmos")
                    or "atmos" in str(params.get("profile", "")).lower()
                    or "joc" in str(params.get("profile", "")).lower()
                )
                writer.Write(get_dec3_box(atmos=atmos))

            if params.get("is_drm_protected"):
                sinf_box = get_sinf_box(kid, "ec-3")
                writer.Write(sinf_box)
                return box("enca", stream.getvalue())
            else:
                return box("ec-3", stream.getvalue())
        else:
            raise NotImplementedError(f"Audio Codec {codec!r} is not supported.")
    elif type == "video":
        writer.WriteUShort(0)
        writer.WriteUShort(0)

        for _ in range(3):
            writer.WriteUInt(0)

        writer.WriteUShort(int(params.get("width", 0) or 0))
        writer.WriteUShort(int(params.get("height", 0) or 0))
        writer.WriteUShort(0x48, padding=2)
        writer.WriteUShort(0x48, padding=2)
        writer.WriteUInt(0)
        writer.WriteUShort(1)

        for _ in range(32):
            writer.WriteByte(0)

        writer.WriteUShort(0x18)
        writer.WriteShort(-1)

        nal_units = [n for n in codec_private_data.split(START_CODE) if n] if codec_private_data and START_CODE in codec_private_data else []

        if codec_lower == "avc1":
            sps = next((n for n in nal_units if n and (n[0] & 0x1F) == 7), None)
            pps = next((n for n in nal_units if n and (n[0] & 0x1F) == 8), None)
            if sps is None or pps is None:
                raise ValueError("Missing SPS or PPS in AVC codec private data")

            avcc_box = get_avcc_box(
                int(params.get("nal_unit_length_field", 4) or 4), sps, pps
            )
            writer.Write(avcc_box)

            if params.get("is_drm_protected"):
                frma_codec = str(params.get("frma_codec") or params.get("sinf_codec") or "avc1").lower()
                sinf_box = get_sinf_box(kid, frma_codec)
                writer.Write(sinf_box)
                return box("encv", stream.getvalue())
            else:
                return box("avc1", stream.getvalue())

        elif codec_lower in ("hvc1", "hev1"):
            vps = next((n for n in nal_units if n and (((n[0] >> 1) & 0x3F) == 32)), None)
            sps = next((n for n in nal_units if n and (((n[0] >> 1) & 0x3F) == 33)), None)
            pps = next((n for n in nal_units if n and (((n[0] >> 1) & 0x3F) == 34)), None)
            if not (vps and sps and pps):
                raise ValueError("Missing VPS, SPS, or PPS in HEVC codec private data")

            hvcc_box = get_hvcc_box(
                int(params.get("nal_unit_length_field", 4) or 4), sps, pps, vps
            )
            writer.Write(hvcc_box)

            if params.get("is_drm_protected"):
                frma_codec = str(params.get("frma_codec") or params.get("sinf_codec") or codec_lower).lower()
                sinf_box = get_sinf_box(kid, frma_codec)
                writer.Write(sinf_box)
                return box("encv", stream.getvalue())
            else:
                return box(codec_lower, stream.getvalue())

        elif codec_lower in ("dvhe", "dvh1"):
            vps = next((n for n in nal_units if n and (((n[0] >> 1) & 0x3F) == 32)), None)
            sps = next((n for n in nal_units if n and (((n[0] >> 1) & 0x3F) == 33)), None)
            pps = next((n for n in nal_units if n and (((n[0] >> 1) & 0x3F) == 34)), None)

            if not (vps and sps and pps):
                raise ValueError("Missing VPS, SPS, or PPS in codec private data")

            hvcc_box = get_hvcc_box(
                int(params.get("nal_unit_length_field", 4) or 4), sps, pps, vps
            )
            dvcc_box = generate_hvcc_dvcc_box()

            writer.Write(hvcc_box)
            writer.Write(dvcc_box)

            clli_box = params.get("clli_box")
            clli_payload = params.get("clli_payload")
            if clli_box is not None:
                if isinstance(clli_box, str):
                    clli_box = bytes.fromhex(clli_box.replace(" ", "").replace("-", ""))
                writer.Write(bytes(clli_box))
            elif clli_payload is not None:
                if isinstance(clli_payload, str):
                    clli_payload = bytes.fromhex(clli_payload.replace(" ", "").replace("-", ""))
                writer.Write(box("clli", bytes(clli_payload)))

            mdcv_box = params.get("mdcv_box")
            mdcv_payload = params.get("mdcv_payload")
            if mdcv_box is not None:
                if isinstance(mdcv_box, str):
                    mdcv_box = bytes.fromhex(mdcv_box.replace(" ", "").replace("-", ""))
                writer.Write(bytes(mdcv_box))
            elif mdcv_payload is not None:
                if isinstance(mdcv_payload, str):
                    mdcv_payload = bytes.fromhex(mdcv_payload.replace(" ", "").replace("-", ""))
                writer.Write(box("mdcv", bytes(mdcv_payload)))

            bitrate = int(params.get("bitrate", 0) or 0)
            if bitrate > 0:
                btrt_stream = io.BytesIO()
                btrt_writer = BinaryWriter(btrt_stream)
                btrt_writer.WriteUInt(bitrate)
                btrt_writer.WriteUInt(bitrate)
                btrt_writer.WriteUInt(bitrate)
                writer.Write(box("btrt", btrt_stream.getvalue()))

            if params.get("is_drm_protected"):
                default_frma = "hvc1" if codec_lower == "dvhe" else codec_lower
                frma_codec = str(params.get("frma_codec") or params.get("sinf_codec") or default_frma).lower()
                sinf_box = get_sinf_box(kid, frma_codec)
                writer.Write(sinf_box)
                return box("encv", stream.getvalue())
            else:
                return box(codec_lower, stream.getvalue())
        else:
            raise NotImplementedError(f"Video Codec {codec!r} is not supported.")

    elif type == "text":
        if codec == "TTML":
            writer.Write("http://www.w3.org/ns/ttml\0")
            writer.Write("\0")
            writer.Write("\0")

            return box("stpp", stream.getvalue())
        else:
            raise NotImplementedError(f"Subtitle Codec {codec!r} is not supported.")
    else:
        raise NotImplementedError(f"Track Type {type!r} is not supported.")


def get_mehd_box(duration):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteULong(duration)

    return full_box("mehd", 1, 0, stream.getvalue())


def get_trex_box(track_id):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteUInt(track_id)
    writer.WriteUInt(1)
    writer.WriteUInt(0)
    writer.WriteUInt(0)
    writer.WriteUInt(0)

    return full_box("trex", 0, 0, stream.getvalue())


def extract_box_data(data, box_sequence):
    data_reader = io.BytesIO(data)
    while True:
        header = data_reader.read(8)
        if len(header) < 8:
            raise ValueError(f"Could not find box path: {box_sequence!r}")

        box_size = u32.unpack(header[:4])[0]
        box_type = header[4:8]

        if box_size == 1:
            largesize = data_reader.read(8)
            if len(largesize) < 8:
                raise ValueError("Invalid large-size MP4 box")
            box_size = int.from_bytes(largesize, byteorder="big")
            header_size = 16
        else:
            header_size = 8

        if box_size < header_size:
            raise ValueError("Invalid MP4 box size")

        payload_size = box_size - header_size

        if box_type == box_sequence[0]:
            box_data = data_reader.read(payload_size)
            if len(box_sequence) == 1:
                return box_data
            return extract_box_data(box_data, box_sequence[1:])

        data_reader.seek(payload_size, 1)


def box(box_type, payload):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteUInt(8 + len(payload))
    writer.Write(box_type.encode("ascii"))
    writer.Write(payload)

    return stream.getvalue()


def full_box(box_type, version, flags, payload):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.Write(version)
    writer.WriteUInt(flags, offset=1)

    return box(box_type, stream.getvalue() + payload)


def get_avcc_box(nal_unit_length_field, sps: bytes, pps: bytes):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteByte(1)
    writer.Write(sps[1:4])
    writer.WriteByte(0xFC | (nal_unit_length_field - 1))
    writer.WriteByte(1)
    writer.WriteUShort(len(sps))
    writer.Write(sps)
    writer.WriteByte(1)
    writer.WriteUShort(len(pps))
    writer.Write(pps)

    return box("avcC", stream.getvalue())

def get_dec3_box(atmos=False):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    payload = b"\x0e\x00\x20\x0f\x00\x01\x10" if atmos else b"\x14\x00\x20\x0f\x00\x00\x00"
    writer.Write(payload)

    return full_box("dec3", 0, 0, stream.getvalue())

def generate_hvcc_dvcc_box():
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    try:
        frame = inspect.currentframe().f_back
        params = frame.f_locals.get("params", {}) if frame else {}
    except Exception:
        params = {}

    raw_box = params.get("dvcc_box") or params.get("dvcC_box") or params.get("dvvC_box")
    if raw_box is not None:
        if isinstance(raw_box, str):
            raw_box = bytes.fromhex(raw_box.replace(" ", "").replace("-", ""))
        raw_box = bytes(raw_box)
        if len(raw_box) >= 8 and raw_box[4:8] in (b"dvcC", b"dvvC"):
            return raw_box

    payload = params.get("dvcc_payload") or params.get("dvcC_payload") or params.get("dvvC_payload")
    if payload is not None:
        if isinstance(payload, str):
            payload = bytes.fromhex(payload.replace(" ", "").replace("-", ""))
        payload = bytes(payload)
        box_type = str(params.get("dv_box_type") or params.get("dv_config_box_type") or "dvcC")
        return box(box_type, payload)

    dv_codec = str(params.get("dv_codec") or params.get("codec_string") or params.get("codec") or "")
    match = re.search(r"(?:dvhe|dvh1|dva1|dvav)\.(\d{2})\.(\d{2})", dv_codec, flags=re.IGNORECASE)

    profile = int(params.get("dv_profile", 5) or 5)
    level = params.get("dv_level")
    if match:
        profile = int(match.group(1))
        if level is None:
            level = int(match.group(2))

    width = int(params.get("width", 0) or 0)
    height = int(params.get("height", 0) or 0)

    if level is None:
        if width > 0 and height > 0:
            if width <= 720 and height <= 576:
                level = 1
            elif width <= 1280 and height <= 720:
                level = 3
            elif width <= 1920 and height <= 1080:
                level = 4
            elif width <= 2560 and height <= 1440:
                level = 5
            elif width <= 3840 and height <= 2160:
                level = 6
            else:
                level = 7
        else:
            level = 6

    level = int(level)

    rpu_present = 1 if params.get("dv_rpu_present", 1) else 0
    el_present = 1 if params.get("dv_el_present", 0) else 0
    bl_present = 1 if params.get("dv_bl_present", 1) else 0
    compatibility_id = int(params.get("dv_compatibility_id", 0) or 0) & 0x0F
    dv_version_major = int(params.get("dv_version_major", 1) or 1) & 0xFF
    dv_version_minor = int(params.get("dv_version_minor", 0) or 0) & 0xFF

    payload = bytearray(24)
    payload[0] = dv_version_major
    payload[1] = dv_version_minor
    payload[2] = ((profile & 0x7F) << 1) | ((level >> 5) & 0x01)
    payload[3] = ((level & 0x1F) << 3) | ((rpu_present & 0x01) << 2) | ((el_present & 0x01) << 1) | (bl_present & 0x01)
    payload[4] = (compatibility_id & 0x0F) << 4

    writer.Write(payload)

    default_box_type = "dvvC" if profile >= 8 else "dvcC"
    box_type = str(params.get("dv_box_type") or params.get("dv_config_box_type") or default_box_type)

    return box(box_type, stream.getvalue())

def get_hvcc_box(nal_unit_length_field, sps: bytes, pps: bytes, vps: bytes):
    ori_sps = bytes(sps)
    enc_list = bytearray()

    with io.BytesIO(sps) as reader:
        while reader.tell() < len(sps):
            byte_value = reader.read(1)
            if not byte_value:
                break
            enc_list.extend(byte_value)
            if len(enc_list) >= 3 and enc_list[-3:] == bytes([0x00, 0x00, 0x03]):
                enc_list.pop()

    sps = bytes(enc_list)

    with io.BytesIO(sps) as reader:
        reader.read(2)
        first_byte = reader.read(1)[0]
        max_sub_layers_minus1 = (first_byte & 0x0E) >> 1
        next_byte = reader.read(1)[0]
        general_profile_space = (next_byte & 0xC0) >> 6
        general_tier_flag = (next_byte & 0x20) >> 5
        general_profile_idc = next_byte & 0x1F
        general_profile_compatibility_flags = int.from_bytes(reader.read(4), byteorder="big")
        constraint_bytes = bytearray(reader.read(6))
        general_level_idc = reader.read(1)[0]

    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteByte(1)
    writer.WriteByte(
        (general_profile_space << 6)
        + (0x20 if general_tier_flag == 1 else 0)
        + general_profile_idc
    )
    writer.WriteUInt(
        general_profile_compatibility_flags
    )
    writer.Write(constraint_bytes)
    writer.WriteByte(general_level_idc)
    writer.WriteUShort(0xF000)
    writer.WriteByte(0xFC)
    writer.WriteByte(0xFC)
    writer.WriteByte(0xF8)
    writer.WriteByte(0xF8)
    writer.WriteUShort(0)
    writer.WriteByte(
        (0 << 6) | (min(max_sub_layers_minus1, 7) << 3) | (0 << 2) | (nal_unit_length_field - 1)
    )
    writer.WriteByte(0x03)

    writer.WriteByte(0x20)
    writer.WriteUShort(1)
    writer.WriteUShort(len(vps))
    writer.Write(vps)
    writer.WriteByte(0x21)
    writer.WriteUShort(1)
    writer.WriteUShort(len(ori_sps))
    writer.Write(ori_sps)
    writer.WriteByte(0x22)
    writer.WriteUShort(1)
    writer.WriteUShort(len(pps))
    writer.Write(pps)

    return box("hvcC", stream.getvalue())

def get_esds_box(track_id, bitrate, codec_private_data: bytes):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    writer.WriteByte(0x03)
    writer.WriteByte(20 + len(codec_private_data))
    writer.WriteByte((track_id & 0xFF00) >> 8)
    writer.WriteByte(track_id & 0x00FF)
    writer.WriteByte(0)

    writer.WriteByte(0x04)
    writer.WriteByte(15 + len(codec_private_data))
    writer.WriteByte(0x40)
    writer.WriteByte((0x05 << 2) | (0 << 1) | 1)

    for _ in range(3):
        writer.WriteByte(0xFF)

    writer.WriteByte((bitrate & 0xFF000000) >> 24)
    writer.WriteByte((bitrate & 0x00FF0000) >> 16)
    writer.WriteByte((bitrate & 0x0000FF00) >> 8)
    writer.WriteByte(bitrate & 0x000000FF)
    writer.WriteByte((bitrate & 0xFF000000) >> 24)
    writer.WriteByte((bitrate & 0x00FF0000) >> 16)
    writer.WriteByte((bitrate & 0x0000FF00) >> 8)
    writer.WriteByte(bitrate & 0x000000FF)

    writer.WriteByte(0x05)
    writer.WriteByte(len(codec_private_data))
    writer.Write(codec_private_data)

    return full_box("esds", 0, 0, stream.getvalue())


def get_sinf_box(key_id, codec):
    key_id = str(key_id or "").replace("-", "").replace(" ", "")
    if key_id.startswith("0x"):
        key_id = key_id[2:]
    key_id = bytes.fromhex(key_id)
    frmaBox = box("frma", codec.encode("ascii"))

    sinfPayload = bytearray()
    sinfPayload.extend(frmaBox)

    schmPayload = bytearray()
    schmPayload.extend(b"cenc")

    schmPayload.extend(
        bytes([0, 1, 0, 0])
    )
    schmBox = full_box("schm", 0, 0, bytes(schmPayload))

    sinfPayload.extend(schmBox)

    tencPayload = bytearray()
    tencPayload.extend(bytes([0, 0]))
    tencPayload.append(TRACK_ENABLED)
    tencPayload.append(0x8)
    tencPayload.extend(key_id)
    tencBox = full_box("tenc", 0, 0, bytes(tencPayload))

    schiBox = box("schi", tencBox)
    sinfPayload.extend(schiBox)

    return box("sinf", bytes(sinfPayload))


def get_playready_pssh_box(pssh):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    protection_data = bytes.fromhex(base64.b64decode(pssh).hex())

    sys_id_data = bytes.fromhex(PLAYREADY_SYSTEM_ID)
    pssh_data = protection_data

    writer.Write(sys_id_data)
    writer.WriteUInt(len(pssh_data))
    writer.Write(pssh_data)

    return full_box("pssh", 0, 0, stream.getvalue())


def get_widevine_pssh_box(key_id):
    stream = io.BytesIO()
    writer = BinaryWriter(stream)

    sys_id_data = bytes.fromhex(WIDEVINE_SYSTEM_ID)
    pssh_data = bytes.fromhex(f"08011210{key_id}1A046E647265220400000000")

    writer.Write(sys_id_data)
    writer.WriteUInt(len(pssh_data))
    writer.Write(pssh_data)

    return full_box("pssh", 0, 0, stream.getvalue())
