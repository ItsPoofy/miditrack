"""nsf2midi/spc2midi/vgm2midiを安全に呼び出し、音源ファイルをMIDIへ変換する。

render.py が midi2wav.sh を呼ぶのと同じ制約・同じ設計: このリポジトリのパス自体が
スペースと '&' を含むため、subprocess.run() に明示的な argv リストを shell=False で
渡し、シェルを一切介さない。バイナリ解決も render.resolve_midi2wav_bin() と同型の
「環境変数（設定済みだが実行不可なら致命的） → リポジトリ相対パス → PATH」の順で行う。

nsf2midi/spc2midi/vgm2midi 自体はJSON出力を持たないため、-l/--list のstdoutを
テキストとしてパースする。パース対象は nsf2midi/src/main.cpp の list_only ブロック
（printf書式）と spc2midi/src/main.cpp の PrintList() であり、いずれもこのリポジトリ
自身が所有する出力なので外部仕様のドリフトは起きない。ただしそれらの printf 書式を
変更したら、ここの正規表現も合わせて直す必要がある。
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import struct
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConvertError, WebValidationError
from .i18n import t
from .libvgm import metadata_path_for as libvgm_metadata_path_for
from .nsf_chip import metadata_path_for as nsf_chip_metadata_path_for
from .tooling import is_executable_file, resolve_resource_root, stderr_tail

CONVERT_TIMEOUT_SECONDS = 300
_STDERR_TAIL_LINES = 20

_SPC_NO_DRIVER_EXIT_CODE = 3
# spc2midi/src/main.cpp の ReportSpcHeaderHints() が出す診断ブロックの見出し。
# 単体の.spc/.spc2 (SPC700ヘッダ+ID666タグを持つ入力) でだけ付く追加行で、
# ゲームタイトル・アーティスト・コメント・ダンパー名・SPC700エントリポイントを
# 含む——対応候補ドライバを後から調べる手がかりになるので、固定の日本語
# メッセージに続けてそのまま残す。壊れた入力では出ないため、その場合は基本
# メッセージのみになる。文字列一致でspc2midi
# 側のstderr整形と結合しているため、main.cppのメッセージ文言を変えたら
# ここも合わせて変更すること(_parse_nsf_list()/_parse_spc_list()と同じ結合)。
_SPC_NO_DRIVER_HINTS_MARKER = "--- ID666 tag"

_M3U_EXTENSIONS = (".m3u", ".m3u8")
_ZIP_EXTENSIONS = (".zip",)

MAX_ARCHIVE_MEMBERS = 200
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class SourceFormat:
    """1つの音源フォーマット（変換元CLIとの対応）。"""

    key: str  # "nsf" | "spc" | "vgm"
    label: str
    extensions: tuple[str, ...]
    env_var: str
    supports_song_list: bool


SOURCE_FORMATS: tuple[SourceFormat, ...] = (
    SourceFormat(
        key="nsf",
        label="NSF / NSFE (ファミコン)",
        extensions=(".nsf", ".nsfe"),
        env_var="NSF2MIDI_BIN",
        supports_song_list=True,
    ),
    SourceFormat(
        key="spc",
        label="SPC / SPC2 (スーパーファミコン)",
        extensions=(".spc", ".spc2"),
        env_var="SPC2MIDI_BIN",
        supports_song_list=True,
    ),
    SourceFormat(
        key="vgm",
        label="VGM / VGZ (メガドライブ・PCエンジン・PC-88/98・ゲームボーイ・アーケード他)",
        extensions=(".vgm", ".vgz"),
        env_var="VGM2MIDI_BIN",
        supports_song_list=False,
    ),
)

_FORMATS_BY_KEY = {fmt.key: fmt for fmt in SOURCE_FORMATS}
_EXTENSION_TO_FORMAT = {
    ext: fmt for fmt in SOURCE_FORMATS for ext in fmt.extensions
}


def format_by_key(key: str) -> SourceFormat:
    """フォーマットキー（"nsf"/"spc"/"vgm"）からSourceFormatを返す。"""
    fmt = _FORMATS_BY_KEY.get(key)
    if fmt is None:
        raise AssertionError(f"unknown format key: {key}")  # pragma: no cover
    return fmt


def try_detect_format(filename: str) -> SourceFormat | None:
    """ファイル名の拡張子からSourceFormatを判定する。未対応拡張子はNone（例外を送出しない）。

    ZIP展開後の候補選別など、対応拡張子かどうかだけを問い合わせたい場面向け。
    """
    suffix = Path(filename).suffix.lower()
    return _EXTENSION_TO_FORMAT.get(suffix)


def detect_format(filename: str) -> SourceFormat:
    """ファイル名の拡張子からSourceFormatを判定する。未対応拡張子はWebValidationError。"""
    fmt = try_detect_format(filename)
    if fmt is None:
        suffix = Path(filename).suffix.lower()
        supported = ", ".join(ext for f in SOURCE_FORMATS for ext in f.extensions)
        raise WebValidationError(
            t(
                "対応していない拡張子です: {suffix}（対応: {supported} / .mid / .midi）",
                suffix=suffix or t("(なし)"),
                supported=supported,
            )
        )
    return fmt


def is_zip_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in _ZIP_EXTENSIONS


def is_m3u_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in _M3U_EXTENSIONS


def is_hidden_member_name(name: str) -> bool:
    """ファイル名（ZIPメンバー名・アップロードされたファイル名）のベースネームがドット（.）で始まるか判定する。

    macOSでZIPを作成すると付随する`__MACOSX/._foo.spc`（AppleDoubleリソースフォーク、
    実データを持たず拡張子だけは本体と一致するため`try_detect_format()`をすり抜けて
    しまう）や`.DS_Store`のような隠しファイルを候補一覧から除外するために使う。
    """
    return Path(name).name.startswith(".")


def _safe_member_path(dest_dir: Path, member_name: str) -> Path:
    """ZIPエントリ名をdest_dir配下の安全な実パスへ変換する（zip-slip対策）。

    ZIPエントリ名は常にスラッシュ区切り（PurePosixPath）である前提。絶対パスや
    '..'を含むパス、正規化した結果がdest_dirの外に出るパスはすべて拒否する。
    """
    posix = PurePosixPath(member_name)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise WebValidationError(t("ZIP内の不正なパスを検出しました: {member_name}", member_name=member_name))
    candidate = (dest_dir / Path(*posix.parts)).resolve()
    if candidate != dest_dir and dest_dir not in candidate.parents:
        raise WebValidationError(t("ZIP内の不正なパスを検出しました: {member_name}", member_name=member_name))
    return candidate


def extract_zip_members(zip_path: Path, dest_dir: Path) -> list[Path]:
    """zip_pathをdest_dir配下に安全に展開し、実際に書き出したファイルパスの一覧を返す。

    zip-slip対策として展開先の実パスがdest_dirの外に出るエントリはすべて拒否する。
    展開前にセントラルディレクトリからファイル数・展開後合計サイズを検査し、上限を
    超える場合は一切展開せずエラーにする（localhost限定・起動スコープトークン認証の
    ローカル専用ツールという既存の信頼モデルに見合った、素朴だが有効なzip爆弾対策）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise WebValidationError(
                    t("ZIP内のファイル数が多すぎます（上限{max_members}）", max_members=MAX_ARCHIVE_MEMBERS)
                )
            total_bytes = sum(info.file_size for info in infos)
            if total_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise WebValidationError(t("ZIPの展開後サイズが大きすぎます"))

            extracted: list[Path] = []
            resolved_dest = dest_dir.resolve()
            for info in infos:
                member_path = _safe_member_path(resolved_dest, info.filename)
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(member_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(member_path)
    except zipfile.BadZipFile as error:
        raise WebValidationError(t("有効なZIPファイルではありません")) from error
    return extracted


# --- m3uプレイリストからの曲名取得 -------------------------------------------


@dataclass(frozen=True)
class M3uEntry:
    """gme(Game_Music_Emu)拡張M3U形式の1エントリ。"""

    file: str
    file_type: str
    track: int | None
    name: str


def _skip_spaces(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] == " ":
        i += 1
    return i


def _parse_m3u_filename_field(s: str, i: int) -> tuple[str, str, int]:
    """`ファイル名[::TYPE]` フィールドを読む。

    カンマの直後（空白を挟んでよい）が数字か'$'ならフィールド終端、そうでなければ
    ファイル名自体に含まれるカンマとして扱う。"::"以降は次のカンマまたは行末までを
    TYPEとして読む。バックスラッシュは直後の1文字をそのまま取り込む（エスケープ）。
    """
    n = len(s)
    out: list[str] = []
    file_type = ""
    while i < n:
        c = s[i]
        i += 1
        if c == ",":
            p = _skip_spaces(s, i)
            if p < n and (s[p] == "$" or s[p].isdigit()):
                i = p
                break
            out.append(c)
            continue
        if c == ":" and i < n and s[i] == ":" and i + 1 < n and s[i + 1] != ",":
            i += 1  # 2つ目の':'を消費
            start = i
            while i < n and s[i] != ",":
                i += 1
            file_type = s[start:i]
            if i < n and s[i] == ",":
                i += 1
                i = _skip_spaces(s, i)
            break
        if c == "\\" and i < n:
            c = s[i]
            i += 1
        out.append(c)
    return "".join(out), file_type, i


def _parse_m3u_track_field(s: str, i: int) -> tuple[int | None, int]:
    """トラック番号フィールドを読む（10進、または'$'接頭辞の16進）。

    長さ・ループ・フェード等の後続フィールドは使わないため、この関数は値だけ読み取り、
    残りは次のカンマまで読み飛ばす。
    """
    n = len(s)
    track: int | None = None
    if i < n and s[i] == "$":
        start = i + 1
        j = start
        while j < n and s[j] in "0123456789abcdefABCDEF":
            j += 1
        if j > start:
            track = int(s[start:j], 16)
        i = j
    else:
        start = i
        while i < n and s[i].isdigit():
            i += 1
        if i > start:
            track = int(s[start:i])
    while i < n and s[i] != ",":
        i += 1
    if i < n and s[i] == ",":
        i += 1
    return track, _skip_spaces(s, i)


def _parse_m3u_name_field(s: str, i: int) -> str:
    n = len(s)
    out: list[str] = []
    while i < n:
        c = s[i]
        i += 1
        if c == ",":
            p = _skip_spaces(s, i)
            if p < n and (s[p] in (",", "-") or s[p].isdigit()):
                i = p
                break
            out.append(c)
            continue
        if c == "\\" and i < n:
            c = s[i]
            i += 1
        out.append(c)
    return "".join(out).strip()


def parse_m3u(text: str) -> list[M3uEntry]:
    """gme(Game_Music_Emu)拡張M3U形式を解析する（`Gme_File::load_m3u()`のPython移植）。

    1行は `ファイル名[::TYPE],トラック,曲名[,長さ[,ループ[,フェード[,リピート]]]]`
    の形式。長さ以降のフィールドは曲名取得には不要なため解析しない。`#`で始まる行は
    Title/Artist等のメタデータコメントとして無視する。ファイル名が空欄の行（`,2,Title`
    のように前行のファイル名を継承する一部の配布慣習）は、直前の非空ファイル名を
    引き継ぐ。
    """
    entries: list[M3uEntry] = []
    last_file = ""
    for raw_line in text.splitlines():
        line = raw_line.strip("\r\n").strip()
        if not line or line.startswith("#"):
            continue
        file, file_type, i = _parse_m3u_filename_field(line, 0)
        if not file:
            file = last_file
        else:
            last_file = file
        if not file:
            continue
        track, i = _parse_m3u_track_field(line, i)
        name = _parse_m3u_name_field(line, i)
        if name:
            entries.append(M3uEntry(file=file, file_type=file_type, track=track, name=name))
    return entries


def filter_m3u_entries(entries: list[M3uEntry], filename: str) -> list[M3uEntry]:
    """指定ファイル名（basename、大小無視）に紐づくエントリだけを元の順序で返す。"""
    target = Path(filename).name.lower()
    return [entry for entry in entries if Path(entry.file).name.lower() == target]


def apply_m3u_titles(songs: list[dict[str, Any]], entries: list[M3uEntry]) -> list[dict[str, Any]]:
    """m3uのエントリから曲名(label)を上書きした新しいsongsリストを返す。

    全エントリにトラック番号がある場合はtrack-1をインデックスとして使う
    （gmeのM3U仕様ではtrackは1始まり。NSF/SPCともnsf2midi/spc2midiの曲インデックスは
    0始まりなので-1する）。トラック番号を欠くエントリが1つでもあれば、信頼できる
    並べ替え情報がないとみなし、m3u内の行順をそのままsongsの並び順に対応付ける
    （通常、配布用m3uは実際の再生順そのままに曲が列挙されているため）。
    """
    if not entries:
        return songs
    updated = [dict(song) for song in songs]
    if all(entry.track is not None for entry in entries):
        for entry in entries:
            assert entry.track is not None
            index = entry.track - 1
            if 0 <= index < len(updated):
                updated[index]["label"] = entry.name
    else:
        for index, entry in enumerate(entries):
            if index < len(updated):
                updated[index]["label"] = entry.name
    return updated


def resolve_converter_argv0(fmt: SourceFormat) -> list[str]:
    """フォーマット別の変換CLIを起動するための前置argvを解決する。

    解決順（render.resolve_midi2wav_bin() と同型）:
      1. <FORMAT>_BIN 環境変数 -- 設定されているのに実行できなければ致命的エラー
         （フォールバックしない）
      2. リポジトリ相対の既知の場所（nsf2midi/spc2midi は素の実行ファイル、
         vgm2midi は dist/cli.js を node で明示的に起動する — シェバン付きだが
         Dropbox同期で実行ビットが落ちうるため）
      3. PATH上のバイナリ（vgm2midiのみ; nsf2midi/spc2midiはPATH上に置かれる
         運用を想定していないため3を持たない）
    """
    env_bin = os.environ.get(fmt.env_var)
    if env_bin:
        if not is_executable_file(env_bin):
            raise ConvertError(f"{fmt.env_var} が実行可能ファイルではありません: {env_bin}")
        return [env_bin]

    repo_root = resolve_resource_root(__file__)

    if fmt.key == "nsf":
        sibling_exe = repo_root / "nsf2midi" / "nsf2midi.exe"
        if is_executable_file(sibling_exe):
            return [str(sibling_exe)]
        sibling = repo_root / "nsf2midi" / "nsf2midi"
        if is_executable_file(sibling):
            return [str(sibling)]
        return ["nsf2midi"]

    if fmt.key == "spc":
        sibling_exe = repo_root / "spc2midi" / "spc2midi.exe"
        if is_executable_file(sibling_exe):
            return [str(sibling_exe)]
        sibling = repo_root / "spc2midi" / "spc2midi"
        if is_executable_file(sibling):
            return [str(sibling)]
        return ["spc2midi"]

    if fmt.key == "vgm":
        cli_js = repo_root / "vgm2midi" / "dist" / "cli.js"
        node_bin = os.environ.get("MIDITRACK_NODE_BIN") or shutil.which("node")
        if cli_js.is_file() and node_bin:
            return [node_bin, str(cli_js)]
        return ["vgm2midi"]

    raise AssertionError(f"unknown format: {fmt.key}")  # pragma: no cover


def _run(argv: list[str], *, tool_label: str) -> subprocess.CompletedProcess[str]:
    try:
        from .tooling import safe_subprocess_run
        return safe_subprocess_run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=CONVERT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise ConvertError(
            f"{tool_label} が見つかりません（{argv[0]}）。{_TOOL_ENV_HINT.get(tool_label, '')}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ConvertError(
            f"{tool_label} の変換が {CONVERT_TIMEOUT_SECONDS} 秒でタイムアウトしました"
        ) from error


_TOOL_ENV_HINT = {
    "nsf2midi": "NSF2MIDI_BIN 環境変数か PATH 上の nsf2midi を確認してください",
    "spc2midi": "SPC2MIDI_BIN 環境変数か PATH 上の spc2midi を確認してください",
    "vgm2midi": "VGM2MIDI_BIN 環境変数、node の有無、または PATH 上の vgm2midi を確認してください",
}


# --- 曲一覧のパース ---------------------------------------------------------

_NSF_TRACK_LINE_RE = re.compile(
    r"^\s*\[\s*(?P<index>\d+)\]\s*(?P<label>.*?)(?:\s*\((?P<sec>\d+(?:\.\d+)?) sec\))?\s*$"
)
_NSF_HEADER_LINE_RE = re.compile(r"^(?P<key>Title|Artist|Copyright|Tracks|Region|Expansion):\s*(?P<value>.*)$")

_SPC_TRACK_LINE_RE = re.compile(
    r'^\s*\[\s*(?P<index>\d+)\]\s+"(?P<label>.*)"\s+driver=(?P<driver>\S+)\s+'
    r"tracks=(?P<tracks>\d+)\s+instrsets=(?P<instrsets>\d+)\s*$"
)
_SPC_HEADER_LINE_RE = re.compile(r"^(?P<key>File|Sequences):\s*(?P<value>.*)$")


def _parse_nsf_list(output: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata: dict[str, Any] = {}
    songs: list[dict[str, Any]] = []
    for line in output.splitlines():
        header_match = _NSF_HEADER_LINE_RE.match(line)
        if header_match:
            metadata[header_match.group("key")] = header_match.group("value").strip()
            continue
        track_match = _NSF_TRACK_LINE_RE.match(line)
        if track_match:
            index = int(track_match.group("index"))
            label = track_match.group("label").strip()
            sec = track_match.group("sec")
            songs.append(
                {
                    "index": index,
                    "label": label or f"Track {index}",
                    "durationSeconds": float(sec) if sec is not None else None,
                    "detail": None,
                }
            )
    return metadata, songs


def _parse_spc_list(output: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata: dict[str, Any] = {}
    songs: list[dict[str, Any]] = []
    for line in output.splitlines():
        header_match = _SPC_HEADER_LINE_RE.match(line)
        if header_match:
            metadata[header_match.group("key")] = header_match.group("value").strip()
            continue
        track_match = _SPC_TRACK_LINE_RE.match(line)
        if track_match:
            index = int(track_match.group("index"))
            label = track_match.group("label").strip()
            songs.append(
                {
                    "index": index,
                    "label": label or f"Sequence {index}",
                    "durationSeconds": None,
                    "detail": (
                        f"driver={track_match.group('driver')} "
                        f"tracks={track_match.group('tracks')} "
                        f"instrsets={track_match.group('instrsets')}"
                    ),
                }
            )
    return metadata, songs


def _spc_no_driver_message(stderr: str) -> str:
    """ドライバ未検出(exit 3)時のConvertErrorメッセージを組み立てる。

    固定の日本語説明に加え、spc2midi自身がstderrへ出す診断情報(見つかれば)
    をそのまま末尾へ残す——ゲームタイトル/アーティスト/コメント/ダンパー名
    (spc2midiが単体.spc/.spc2のID666タグから読む)とSPC700エントリポイント。
    これらは「このドライバが対応済みドライバの亜種か」「新規対応を追加する
    ならどこから解析すればよいか」を後から調べる際の手がかりになる。
    """
    message = (
        "対応するSNESサウンドドライバが見つかりませんでした。"
        "このSPCファイルの音楽ドライバはspc2midiが解析できる20種類のいずれにも該当しません。"
    )
    index = stderr.find(_SPC_NO_DRIVER_HINTS_MARKER)
    if index == -1:
        return message
    hints = stderr[index:].strip()
    return f"{message}\n{hints}"


def list_songs(fmt: SourceFormat, source_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """<tool> -l/--list を実行し、(メタデータ, 曲一覧) を返す。

    supports_song_list=False のフォーマット（vgm2midi）には呼ばない。
    """
    if not fmt.supports_song_list:
        raise AssertionError(f"{fmt.key} does not support song listing")  # pragma: no cover

    argv0 = resolve_converter_argv0(fmt)
    argv = [*argv0, "-l", str(source_path)]
    result = _run(argv, tool_label=f"{fmt.key}2midi")

    if fmt.key == "spc" and result.returncode == _SPC_NO_DRIVER_EXIT_CODE:
        raise ConvertError(_spc_no_driver_message(result.stderr))
    if result.returncode != 0:
        tail = stderr_tail(result.stderr, _STDERR_TAIL_LINES)
        raise ConvertError(f"{fmt.key}2midi の曲一覧取得に失敗しました（exit={result.returncode}）:\n{tail}")

    if fmt.key == "nsf":
        return _parse_nsf_list(result.stdout)
    return _parse_spc_list(result.stdout)


# --- 変換オプションスキーマ --------------------------------------------------


def probe_tempo(fmt: SourceFormat, source_path: Path) -> int | None:
    """フォーマットが対応していれば、音源ファイルからテンポ（BPM）を高速プローブして返す。"""
    if fmt.key != "vgm":
        return None
    try:
        argv0 = resolve_converter_argv0(fmt)
        argv = [*argv0, "--probe", str(source_path)]
        result = _run(argv, tool_label=f"{fmt.key}2midi")
        if result.returncode == 0 and result.stdout.strip():
            import json
            data = json.loads(result.stdout.strip())
            tempo = data.get("tempo")
            if isinstance(tempo, (int, float)) and tempo > 0:
                return round(tempo)
    except Exception:
        pass
    return None


def detect_vgm_console(path: Path) -> dict[str, str] | None:
    """VGM/VGZヘッダおよびGD3タグからハードウェア・ゲーム機（コンソール）名を判定する。"""
    try:
        raw = path.read_bytes()
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        if len(data) < 64 or data[:4] != b"Vgm ":
            return None
        version = struct.unpack("<I", data[8:12])[0]
        gd3_offset = struct.unpack("<I", data[0x14:0x18])[0]
        sys_en = ""
        sys_jp = ""
        if gd3_offset != 0:
            gd3_start = 0x14 + gd3_offset
            if gd3_start + 12 <= len(data) and data[gd3_start : gd3_start + 4] == b"Gd3 ":
                gd3_len = struct.unpack("<I", data[gd3_start + 8 : gd3_start + 12])[0]
                gd3_data = data[gd3_start + 12 : gd3_start + 12 + gd3_len]
                strings = []
                cur = bytearray()
                for i in range(0, len(gd3_data), 2):
                    char = gd3_data[i : i + 2]
                    if char == b"\x00\x00":
                        strings.append(cur.decode("utf-16le", errors="replace").strip())
                        cur = bytearray()
                    else:
                        cur.extend(char)
                if len(strings) > 4:
                    sys_en = strings[4]
                if len(strings) > 5:
                    sys_jp = strings[5]

        clean_name = sys_jp or sys_en
        if clean_name:
            cn = clean_name.lower()
            if "mega drive" in cn or "genesis" in cn or "メガドライブ" in cn:
                return {"ja": "メガドライブ", "en": "Genesis / Mega Drive"}
            if "pc-98" in cn or "pc98" in cn:
                return {"ja": "PC-98", "en": "PC-98"}
            if "pc-88" in cn or "pc88" in cn:
                return {"ja": "PC-88", "en": "PC-88"}
            if "pc engine" in cn or "pcエンジン" in cn or "turbografx" in cn:
                return {"ja": "PCエンジン", "en": "PC Engine / TG-16"}
            if "game boy" in cn or "gameboy" in cn or "ゲームボーイ" in cn:
                return {"ja": "ゲームボーイ", "en": "Game Boy"}
            if "master system" in cn or "マスターシステム" in cn:
                return {"ja": "マスターシステム", "en": "Master System"}
            if "game gear" in cn or "ゲームギア" in cn:
                return {"ja": "ゲームギア", "en": "Game Gear"}
            if "x68000" in cn:
                return {"ja": "X68000", "en": "X68000"}
            if "arcade" in cn or "アーケード" in cn:
                return {"ja": "アーケード", "en": "Arcade"}
            if "famicom" in cn or "nes" in cn or "ファミコン" in cn:
                return {"ja": "ファミコン", "en": "NES / Famicom"}
            return {"ja": clean_name, "en": sys_en or clean_name}

        def read_clock(offset: int, min_ver: int) -> int:
            return (
                struct.unpack("<I", data[offset : offset + 4])[0]
                if version >= min_ver and len(data) >= offset + 4
                else 0
            )

        sn = struct.unpack("<I", data[0x0C:0x10])[0]
        ym2413 = struct.unpack("<I", data[0x10:0x14])[0]
        ym2612 = read_clock(0x2C, 0x110)
        ym2151 = read_clock(0x30, 0x110)
        sega_pcm = read_clock(0x38, 0x151)
        ym2203 = read_clock(0x44, 0x151)
        ym2608 = read_clock(0x48, 0x151)
        ym2610 = read_clock(0x4C, 0x151)
        gb = read_clock(0x80, 0x161)
        nes = read_clock(0x84, 0x161)
        huc = read_clock(0xA4, 0x161)
        c140 = read_clock(0xA8, 0x161)
        qsound = read_clock(0xB4, 0x161)

        if ym2612 or sega_pcm:
            return {"ja": "メガドライブ", "en": "Genesis / Mega Drive"}
        if ym2608:
            return {"ja": "PC-88 / PC-98", "en": "PC-88 / PC-98"}
        if ym2203:
            return {"ja": "PC-88 / アーケード", "en": "PC-88 / Arcade"}
        if ym2610:
            return {"ja": "ネオジオ / アーケード", "en": "Neo Geo / Arcade"}
        if huc:
            return {"ja": "PCエンジン", "en": "PC Engine / TG-16"}
        if gb:
            return {"ja": "ゲームボーイ", "en": "Game Boy"}
        if nes:
            return {"ja": "ファミコン", "en": "NES / Famicom"}
        if ym2151:
            return {"ja": "アーケード / X68000", "en": "Arcade / X68000"}
        if qsound or c140:
            return {"ja": "アーケード", "en": "Arcade"}
        if sn or ym2413:
            return {"ja": "マスターシステム / ゲームギア", "en": "Master System / Game Gear"}
        return None
    except Exception:
        return None


def option_schema(fmt: SourceFormat, detected_tempo: int | None = None) -> list[dict[str, Any]]:
    """フォーマット別の変換オプション定義（フロントエンドが動的に描画する）。"""
    if fmt.key == "nsf":
        return [
            {"name": "songIndex", "type": "song", "label": t("曲"), "default": 0},
            {
                "name": "loops",
                "type": "number",
                "label": t("ループ回数"),
                "default": None,
                "layoutGroup": "timing",
                "unavailable": True,
                "placeholder": t("指定不可"),
            },
            {
                "name": "durationSeconds",
                "type": "number",
                "label": t("曲の長さ（秒）"),
                "default": None,
                "min": 1,
                "layoutGroup": "timing",
                "placeholder": t("自動"),
            },
            {
                "name": "chipNoise",
                "type": "bool",
                "label": t("原曲の音源を優先"),
                "default": False,
            },
            {"name": "forcePal", "type": "bool", "label": t("PAL方式 (50Hz)"), "default": False},
        ]
    if fmt.key == "spc":
        return [
            {"name": "songIndex", "type": "song", "label": t("曲"), "default": 0},
            {
                "name": "loops",
                "type": "number",
                "label": t("ループ回数"),
                "default": 1,
                "min": 0,
                "layoutGroup": "timing",
            },
            {
                "name": "durationSeconds",
                "type": "number",
                "label": t("曲の長さ（秒）"),
                "default": None,
                "layoutGroup": "timing",
                "unavailable": True,
                "placeholder": t("指定不可"),
            },
            {
                "name": "gameSoundfont",
                "type": "bool",
                "label": t("原曲のSoundFontを優先"),
                "default": False,
            },
        ]
    if fmt.key == "vgm":
        return [
            {
                "name": "loops",
                "type": "number",
                "label": t("ループ回数"),
                "default": None,
                "min": 1,
                "layoutGroup": "timing",
                "conflicts": ["durationSeconds"],
                "placeholder": t("自動"),
            },
            {
                "name": "durationSeconds",
                "type": "number",
                "label": t("曲の長さ（秒）"),
                "default": None,
                "min": 0.001,
                "layoutGroup": "timing",
                "conflicts": ["loops"],
                "placeholder": t("自動"),
            },
            {
                "name": "chipNoise",
                "type": "bool",
                "label": t("原曲の音源を優先"),
                "default": False,
            },
            {
                "name": "ch3SpecialPercussion",
                "type": "bool",
                "label": t("OPN Ch3ドラム変換"),
                "default": False,
            },
            {
                "name": "preserveChipTuning",
                "type": "bool",
                "label": t("実機の微小デチューンを保持"),
                "default": False,
            },
            {
                "name": "autoTempo",
                "type": "bool",
                "label": t("テンポ自動検出"),
                "default": True,
            },
            {
                "name": "tempo",
                "type": "number",
                "label": t("手動テンポ (BPM)"),
                "default": None,
                "min": 20,
                "max": 500,
                "conflicts": ["autoTempo"],
                "placeholder": str(detected_tempo) if detected_tempo is not None else "120",
            },
        ]
    raise AssertionError(f"unknown format: {fmt.key}")  # pragma: no cover


def validate_convert_options(fmt: SourceFormat, songs: list[dict[str, Any]], raw: dict[str, Any]) -> dict[str, Any]:
    """クライアントから送られてきた変換オプションをサーバー側で独立に検証する。"""
    schema = {field["name"]: field for field in option_schema(fmt)}
    result: dict[str, Any] = {}

    for name, field in schema.items():
        if field.get("unavailable"):
            # クライアント側のdisabledだけを信用しない。この形式では指定
            # できない項目なので、型が壊れていようが何が送られてきても
            # 常に既定値へ潰す（意味不明な400を返すより黙って無視する方が
            # 正しい——そもそも指定できないと案内している項目のため）。
            result[name] = field.get("default")
            continue
        if name not in raw or raw[name] is None or raw[name] == "":
            if field["type"] == "song" and songs:
                result[name] = 0
            else:
                result[name] = None if field.get("default") is None else field["default"]
            continue
        value = raw[name]
        if field["type"] == "song":
            if not isinstance(value, int) or isinstance(value, bool):
                raise WebValidationError(t("{name}は整数で指定してください", name=name))
            if not songs or not (0 <= value < len(songs)):
                raise WebValidationError(t("曲番号が範囲外です: {value}", value=value))
            result[name] = value
        elif field["type"] == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise WebValidationError(t("{name}は数値で指定してください", name=name))
            minimum = field.get("min")
            if minimum is not None and value < minimum:
                raise WebValidationError(t("{name}は{minimum}以上で指定してください", name=name, minimum=minimum))
            result[name] = value
        elif field["type"] == "bool":
            result[name] = bool(value)
        else:  # pragma: no cover
            raise AssertionError(f"unknown field type: {field['type']}")

    for name, field in schema.items():
        conflicts = field.get("conflicts")
        val = result.get(name)
        if not conflicts or val is None or val is False:
            continue
        for other in conflicts:
            other_val = result.get(other)
            if other_val is not None and other_val is not False:
                raise WebValidationError(t("{name}と{other}は同時に指定できません", name=name, other=other))

    return result


# --- 変換実行 -----------------------------------------------------------------


def chip_stem_path_for(output_path: Path) -> Path:
    """変換先の .mid パスから、実機ノイズ/DPCMステムWAVのパスを導出する。

    web.py は常に同じ固定パス（converted.mid）へ変換するため、このパスも常に
    同じ固定パス（converted.chip.wav）になる。convert_to_midi() は実行前に
    必ずこのパスを unlink するが、これは前回の変換で生成されたステムが
    「今回の曲のノイズ」として誤ってミックスされることを防ぐための必須の処理
    であり、単なる後片付けではない。
    """
    return output_path.with_name(output_path.stem + ".chip.wav")


def dac_stem_path_for(output_path: Path) -> Path:
    """変換先の .mid パスから、vgm2midi --dac-wav が書き出すYM2612 DACステムWAVのパスを
    導出する。chip_stem_path_for() と同じ「固定出力パスから固定副産物パスを導出する」
    パターンだが、vgm2midiでは --noise-wav（chip_stem_path_for）と --dac-wav
    （こちら）が独立したCLIオプション・独立したステムなので、拡張子直前を別の
    サフィックス（.dac.wav）にして両者が衝突しないようにしている。
    """
    return output_path.with_name(output_path.stem + ".dac.wav")


# spc2midi --sf2 が書き出すSF2のバイト数の下限。RIFF/sfbkのヘッダだけでもこれを
# 超えるため、「ファイルは存在するが実質空」を弾く実用的な閾値として使う。
_MIN_GAME_SOUNDFONT_BYTES = 64


def game_soundfont_path_for(output_path: Path) -> Path:
    """変換先の .mid パスから、spc2midi --sf2 が書き出すSoundFontのパスを導出する。

    spc2midi/src/main.cpp の ConvertOne() は
    SaveSf2(coll, spc2midi::ReplaceExtension(mid_path, "sf2")) を呼ぶ
    （spc2midi/src/paths.cpp の ReplaceExtension() は最後の '.' 以降を
    置換するだけ）。web.py は常に固定パス converted.mid へ変換するため、
    このパスも常に固定パス converted.sf2 になる — chip_stem_path_for() と
    同じ「固定出力パスから固定副産物パスを導出する」パターン。
    """
    return output_path.with_suffix(".sf2")


def produced_game_soundfont(output_path: Path) -> Path | None:
    """変換後、spc2midiが実際にゲーム由来SoundFontを書き出していればそのパスを返す。

    --sf2 は gameSoundfont オプションの有無に関わらず常に要求する
    （_build_argv()参照）ため、ここでの判定はオプション値ではなく生成物の
    存在だけを見ればよい。spc2midiは instrSets() が空のとき警告のみでSF2を
    書かず、終了コードは0のままになる（main.cpp のSaveSf2()）ため、
    「SF2が生成されなかった」が正常系として起こりうる ― 存在とサイズで判定する。
    """
    sf2_path = game_soundfont_path_for(output_path)
    if sf2_path.exists() and sf2_path.stat().st_size > _MIN_GAME_SOUNDFONT_BYTES:
        return sf2_path
    return None


def _build_argv(
    fmt: SourceFormat, source_path: Path, output_path: Path, options: dict[str, Any]
) -> list[str]:
    argv0 = resolve_converter_argv0(fmt)

    if fmt.key == "nsf":
        argv = [*argv0, "-t", str(options["songIndex"])]
        if options.get("durationSeconds") is not None:
            argv += ["-d", str(options["durationSeconds"])]
        if options.get("forcePal"):
            argv.append("--pal")
        argv += ["--track-metadata", str(nsf_chip_metadata_path_for(output_path))]
        argv += [str(source_path), str(output_path)]
        return argv

    if fmt.key == "spc":
        # NSF/VGMが --track-metadata を常に要求してトラック単位の"game"選択を
        # 常時使えるようにしているのと同じ理由で、--sf2 も gameSoundfont の
        # チェック有無に関わらず常に要求する。gameSoundfont自体は「音符のある
        # 全トラックを初期選択するかどうか」だけを制御する
        # （produced_game_soundfont()/convert_source()参照）。
        argv = [
            *argv0,
            "-s",
            str(options["songIndex"]),
            "--loops",
            str(options.get("loops", 1)),
            "--sf2",
        ]
        argv += [str(source_path), str(output_path)]
        return argv

    if fmt.key == "vgm":
        argv = [*argv0, "-o", str(output_path)]
        if options.get("tempo") is not None:
            argv += ["-t", str(options["tempo"])]
        elif options.get("autoTempo", True):
            argv.append("--auto-tempo")
        else:
            argv += ["-t", "120"]
        argv += ["--track-metadata", str(libvgm_metadata_path_for(output_path))]
        if options.get("loops") is not None:
            argv += ["--loops", str(options["loops"])]
        elif options.get("durationSeconds") is not None:
            argv += ["--duration", str(options["durationSeconds"])]
        if options.get("chipNoise"):
            argv += ["--noise-wav", str(chip_stem_path_for(output_path))]
            argv.append("--keep-noise-midi")
            argv += ["--dac-wav", str(dac_stem_path_for(output_path))]
            argv.append("--keep-dac-midi")
        if options.get("ch3SpecialPercussion"):
            argv.append("--ch3-special-percussion")
        if not options.get("preserveChipTuning", False):
            argv.append("--no-chip-tuning")
        argv.append(str(source_path))
        return argv

    raise AssertionError(f"unknown format: {fmt.key}")  # pragma: no cover


def convert_to_midi(
    fmt: SourceFormat, source_path: Path, output_path: Path, options: dict[str, Any]
) -> tuple[Path | None, Path | None]:
    """音源ファイルをMIDIに変換する。失敗時は ConvertError。

    chipNoise オプションが有効で、実機ノイズ/DPCM(nsf、後方互換の旧経路のみ)
    またはLFSRノイズ(vgm) ステムWAVが実際に生成された場合は1つ目にそのパスを
    返す。vgmではchipNoise有効時さらにYM2612 DAC(PCM)サンプルステムも独立して
    生成しうるので、2つ目にそのパスを返す（nsfには対応するチャンネルが無いため
    常にNone）。生成されなかった側はそれぞれNone。

    NSF/VGMとも --track-metadata sidecar を常に要求するため、通常はこの
    タプルはどちらもNoneになり、トラックごとの音源選択（"game"/"soundfont"）は
    web.py がsidecar経由で扱う。sidecarを書かない旧nsf2midiバイナリと接続した
    場合のみ、NSFの戻り値に旧chip_stem_pathが入る後方互換経路が生きる。
    """
    stem_path = chip_stem_path_for(output_path)
    dac_path = dac_stem_path_for(output_path)
    sf2_path = game_soundfont_path_for(output_path)
    vgm_metadata_path = libvgm_metadata_path_for(output_path)
    nsf_metadata_path = nsf_chip_metadata_path_for(output_path)
    # 前回変換のステム/SF2/sidecarが残っていると、今回の変換が失敗した/該当
    # オプションを外した場合に「前の曲のノイズ」「前の曲の音色」「前の曲の
    # チャンネル対応」が誤って使われてしまう。実行前に必ず消す。
    stem_path.unlink(missing_ok=True)
    dac_path.unlink(missing_ok=True)
    sf2_path.unlink(missing_ok=True)
    vgm_metadata_path.unlink(missing_ok=True)
    nsf_metadata_path.unlink(missing_ok=True)

    argv = _build_argv(fmt, source_path, output_path, options)
    tool_label = f"{fmt.key}2midi"
    result = _run(argv, tool_label=tool_label)

    if fmt.key == "spc" and result.returncode == _SPC_NO_DRIVER_EXIT_CODE:
        raise ConvertError(_spc_no_driver_message(result.stderr))
    if result.returncode != 0:
        tail = stderr_tail(result.stderr, _STDERR_TAIL_LINES)
        raise ConvertError(f"{tool_label} の変換に失敗しました（exit={result.returncode}）:\n{tail}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ConvertError("MIDIが生成されませんでした")

    if not options.get("chipNoise"):
        return None, None

    def produced(path: Path) -> Path | None:
        return path if path.exists() and path.stat().st_size > 44 else None

    return produced(stem_path), produced(dac_path)
