"""샤오홍슈 / 더우인 영상 다운로더 (최대 1080p, 워터마크 없음 - 아래 MAX_RES로 변경)

사용법:
  python vdl.py                 -> URL(또는 공유 문구 통째로) 붙여넣기 반복 모드
                                   붙여넣는 즉시 백그라운드에서 동시에 받으니 계속 붙여넣으면 됨
                                   (여러 줄 한꺼번에 붙여넣기도 OK)
  python vdl.py URL [URL ...]   -> 바로 다운로드
  python vdl.py 목록.txt         -> 텍스트 파일 안의 URL 전부 다운로드

파일명은 붙여넣은 순서대로 1.mp4, 2.mp4 ... (사이트별 폴더마다 따로 번호)
어떤 번호가 어떤 영상인지는 각 폴더의 목록.txt 에 기록됨
"""
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

import requests

# 이 파일이 있는 폴더에 douyin / xiaohongshu 폴더를 만들어 저장
SAVE_DIR = Path(__file__).resolve().parent
TMP_DIR = SAVE_DIR / ".tmp"  # 다운로드/변환 중인 파일 (끝나면 최종 파일만 옮겨짐)
FAILED_FILE = SAVE_DIR / "실패목록.txt"
LIST_NAME = "목록.txt"  # 폴더별: 번호 / 영상ID / 제목 / URL
# 최대 해상도 (짧은 변 기준, 1080 = 1080p). 원본 그대로 받으려면 None
MAX_RES = 1080
# 동시에 받을 개수 (너무 늘리면 더우인/샤오홍슈가 차단할 수 있음)
WORKERS = 3
# 실패하면 몇 번까지 시도할지
RETRIES = 3

_print_lock = threading.Lock()
_num_lock = threading.Lock()
_next_num = {}
_convert_lock = threading.Lock()  # ffmpeg는 혼자서도 CPU를 다 쓰므로 변환은 하나씩
_job = threading.local()

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

URL_RE = re.compile(r"https?://[^\s一-鿿，。！]+")


class Permanent(Exception):
    """다시 시도해도 소용없는 오류 (지원 안 하는 링크 등)"""


def log(msg):
    tag = getattr(_job, "tag", "")
    with _print_lock:
        print(f"{tag} {msg}" if tag else msg, flush=True)


def safe_name(s, limit=60):
    s = re.sub(r'[\\/:*?"<>|\r\n\t#]+', " ", s or "").strip()
    return (s[:limit].strip() or "video")


def resolve(url, ua):
    r = requests.get(url, headers={"User-Agent": ua}, allow_redirects=True, timeout=15)
    return r.url


def site_folder(url):
    if "douyin" in url:
        return "douyin"
    if "xiaohongshu" in url or "xhslink" in url:
        return "xiaohongshu"
    return None


# ---------------- 번호 / 목록 ----------------
def read_list(folder):
    f = SAVE_DIR / folder / LIST_NAME
    if not f.exists():
        return []
    return [line.split("\t") for line in f.read_text(encoding="utf-8", errors="replace").splitlines() if line]


def next_number(folder):
    """폴더 안의 기존 번호(파일 + 목록.txt) 다음 번호를 붙여넣은 순서대로 배정"""
    with _num_lock:
        if folder not in _next_num:
            used = [0]
            d = SAVE_DIR / folder
            if d.exists():
                for p in d.iterdir():
                    m = re.match(r"(\d+)(?:_\d+)?\.\w+$", p.name)
                    if m:
                        used.append(int(m.group(1)))
            for row in read_list(folder):
                m = re.match(r"(\d+)", row[0])
                if m:
                    used.append(int(m.group(1)))
            _next_num[folder] = max(used) + 1
        n = _next_num[folder]
        _next_num[folder] += 1
        return n


def record(folder, files, item_id, title):
    with _num_lock:
        with open(SAVE_DIR / folder / LIST_NAME, "a", encoding="utf-8") as f:
            f.write(f"{files}\t{item_id}\t{title}\t{_job.url}\n")


def already_have(folder, item_id):
    """같은 영상을 이미 받았으면 그 파일 이름을 돌려줌"""
    for row in read_list(folder):
        if len(row) > 1 and row[1] == item_id:
            return row[0]
    for p in (SAVE_DIR / folder).glob(f"*_{item_id}*"):  # 예전 방식(제목_ID.mp4)으로 받은 파일
        return p.name
    return None


def stage_path(ext, idx=0):
    """받는 중인 파일은 임시 폴더에 작업번호로 두고, 저장할 때 순서대로 번호를 붙임"""
    return TMP_DIR / f"job{_job.n}_{idx}{ext}"


# ---------------- 다운로드 / 변환 ----------------
def download(url, path, referer, video=False):
    """임시 폴더에 받고(영상이면 변환까지) 끝나면 path로 옮김"""
    TMP_DIR.mkdir(exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    headers = {"User-Agent": DESKTOP_UA, "Referer": referer}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            if total:
                log(f"다운로드 중 ({total / 1e6:.0f}MB)")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        if video:
            shrink(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def shrink(path):
    """MAX_RES보다 큰 영상이면 MAX_RES로 줄여서 덮어씀"""
    if not MAX_RES:
        return
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        log("(ffmpeg가 없어 원본 해상도로 저장함)")
        return
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace").stderr
    m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", probe)
    if not m or min(int(m.group(1)), int(m.group(2))) <= MAX_RES:
        return
    if _convert_lock.locked():
        log("다른 영상 변환이 끝나길 기다리는 중...")
    with _convert_lock:
        log(f"{m.group(1)}x{m.group(2)} -> {MAX_RES}p 로 변환 중... (영상 길이에 따라 수십 초)")
        tmp = path.with_name(path.stem + ".converting.mp4")
        vf = f"scale='if(gt(iw,ih),-2,{MAX_RES})':'if(gt(iw,ih),{MAX_RES},-2)'"
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vf", vf,
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "copy",
                            "-movflags", "+faststart", str(tmp)], capture_output=True)
    if r.returncode == 0 and tmp.exists():
        before = path.stat().st_size
        os.replace(tmp, path)
        log(f"변환 완료: {before / 1e6:.0f}MB -> {path.stat().st_size / 1e6:.0f}MB")
    else:
        tmp.unlink(missing_ok=True)
        log("변환 실패 - 원본 해상도로 저장함")


# ---------------- 더우인 ----------------
def mp4_info(url):
    """앞부분만 받아서 (해상도 짧은 변, 전체 바이트수) 확인"""
    r = requests.get(url, headers={"User-Agent": DESKTOP_UA, "Range": "bytes=0-400000"}, timeout=20)
    if r.status_code not in (200, 206) or "video" not in r.headers.get("Content-Type", ""):
        return None
    total = int((r.headers.get("Content-Range") or "/0").split("/")[-1] or 0)
    d, best, i = r.content, 0, r.content.find(b"tkhd")
    while i != -1:
        off = i + 4 + (84 if d[i + 4] == 1 else 72) + 4
        w, h = struct.unpack(">II", d[off:off + 8])
        best = max(best, min(w >> 16, h >> 16))
        i = d.find(b"tkhd", i + 4)
    return best, total


def douyin_page(url):
    """더우인은 서명 없는 요청을 막아서, 설치된 Edge/Chrome을 백그라운드로 띄워 영상 정보를 얻음"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = None
        for ch in ("msedge", "chrome"):
            try:
                browser = p.chromium.launch(channel=ch, headless=True)
                break
            except Exception:
                continue
        if not browser:
            raise Permanent("Edge 또는 Chrome이 필요합니다")
        page = browser.new_page()
        detail = {}

        def on_resp(r):
            if "aweme/detail" in r.url:
                try:
                    d = r.json().get("aweme_detail")
                    if d and d.get("aweme_id") == url.rstrip("/").split("/")[-1]:
                        detail["d"] = d
                except Exception:
                    pass
        page.on("response", on_resp)
        html = ""
        # 더우인이 가끔 메인페이지로 튕겨내서, 같은 브라우저(쿠키 유지)로 잠깐 쉬었다가 재시도
        for attempt in range(5):
            if attempt:
                page.wait_for_timeout(1500 * attempt)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                continue
            for _ in range(20):  # 최대 10초 대기
                page.wait_for_timeout(500)
                try:
                    html = page.content()
                except Exception:  # 페이지가 리다이렉트 중
                    continue
                if "d" in detail or "video_id=" in html or "/video/" not in page.url:
                    break
            if "d" in detail or "video_id=" in html:
                break
        try:
            title = page.title()
        except Exception:
            title = ""
        browser.close()
    return detail.get("d"), html, title


def douyin(url):
    full = resolve(url, MOBILE_UA)
    m = re.search(r"(?:video|note|slides)/(\d+)|modal_id=(\d+)|aweme_id=(\d+)", full)
    if not m:
        raise Permanent(f"더우인 영상 ID를 찾을 수 없음: {full}")
    vid = next(g for g in m.groups() if g)
    if have := already_have("douyin", vid):
        log(f"이미 받은 영상이라 건너뜀: {have}")
        return "skip"
    log("영상 정보 가져오는 중...")
    for _ in range(2):  # 그래도 안 되면 브라우저를 새로 띄워서 한 번 더
        item, html, page_title = douyin_page(f"https://www.douyin.com/video/{vid}")
        if item or "video_id=" in html:
            break

    ref = "https://www.douyin.com/"
    if item:
        title = safe_name(f"{item.get('author', {}).get('nickname', '')}_{item.get('desc', '')}", 100)
    else:
        title = safe_name(re.sub(r"\s*-\s*抖音\s*$", "", page_title), 100)

    if item and item.get("images"):  # 이미지 게시물
        files = []
        for i, img in enumerate(item["images"], 1):
            files.append(stage_path(".jpg", i))
            download(img["url_list"][-1], files[-1], ref)
        return files, vid, title

    uri = (item or {}).get("video", {}).get("play_addr", {}).get("uri")
    if not uri:
        m = re.search(r"video_id=([0-9a-z]+)", html)
        if not m:
            raise RuntimeError("더우인 영상 주소를 찾지 못함 (삭제/비공개 또는 일시적 차단)")
        uri = m.group(1)

    # ratio 값마다 서버가 주는 파일이 달라서(1080p 요청이 540p로 떨어지기도 함) 전부 확인 후
    # MAX_RES 이하 중 최고를 고름 (없으면 가장 큰 것을 받아서 줄임)
    best = None
    for ratio in ("2160p", "1440p", "1080p", "720p", "540p"):
        u = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio={ratio}&line=0"
        try:
            info = mp4_info(u)
        except requests.RequestException:
            continue
        if not info:
            continue
        key = (not MAX_RES or info[0] <= MAX_RES, info[0], info[1])
        if best is None or key > best[0]:
            best = (key, u)
    if not best:
        raise RuntimeError("더우인 영상 다운로드 주소 확인 실패")
    out = stage_path(".mp4")
    download(best[1], out, ref, video=True)
    return [out], vid, title


# ---------------- 샤오홍슈 ----------------
def xhs_note(page_url, ua, note_id):
    html = requests.get(page_url, headers={"User-Agent": ua, "Referer": "https://www.xiaohongshu.com/"},
                        timeout=15).text
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        return None
    state = json.loads(re.sub(r"\bundefined\b", "null", m.group(1)))
    # 모바일 페이지 구조
    note = (((state.get("noteData") or {}).get("data") or {}).get("noteData"))
    if note and note.get("noteId") == note_id:
        return note
    # PC 페이지 구조
    return (((state.get("note") or {}).get("noteDetailMap") or {}).get(note_id) or {}).get("note")


def xhs(url):
    # PC로 접속하면 로그인 페이지로 튕기므로 모바일로 접속 (로그인 불필요)
    full = unquote(resolve(url, MOBILE_UA))
    m = re.search(r"/(?:explore|discovery/item|item)/([0-9a-f]{24})(\?[^\s#]*)?", full)
    if not m:
        raise Permanent(f"샤오홍슈 노트 ID를 찾을 수 없음: {full}")
    note_id, query = m.group(1), m.group(2) or ""
    if have := already_have("xiaohongshu", note_id):
        log(f"이미 받은 영상이라 건너뜀: {have}")
        return "skip"

    detail = None
    for page_url, ua in ((f"https://www.xiaohongshu.com/discovery/item/{note_id}{query}", MOBILE_UA),
                         (f"https://www.xiaohongshu.com/explore/{note_id}{query}", DESKTOP_UA)):
        try:
            detail = xhs_note(page_url, ua, note_id)
        except requests.RequestException:
            continue
        if detail:
            break
    if not detail:
        raise RuntimeError("샤오홍슈 노트 정보 없음 (공유 링크를 xsec_token 포함해서 통째로 붙여넣어 주세요)")

    user = detail.get("user") or {}
    author = user.get("nickname") or user.get("nickName") or ""
    title = safe_name(f"{author}_{detail.get('title') or detail.get('desc', '')}", 100)
    ref = "https://www.xiaohongshu.com/"

    video = detail.get("video")
    if not video:  # 이미지 노트
        files = []
        for i, img in enumerate(detail.get("imageList", []), 1):
            files.append(stage_path(".jpg", i))
            download(img.get("urlDefault") or img.get("url"), files[-1], ref)
        return files, note_id, title

    out = stage_path(".mp4")
    streams = []
    for codec, lst in (video.get("media", {}).get("stream") or {}).items():
        for s in lst or []:
            if s.get("masterUrl"):
                streams.append(s)
    short = lambda s: min(s.get("width") or 0, s.get("height") or 0)
    streams.sort(key=lambda s: (short(s), s.get("videoBitrate") or s.get("avgBitrate") or 0), reverse=True)

    candidates = []
    # 1순위: 딱 MAX_RES 화질 스트림이 있으면 그대로 (변환 불필요)
    if MAX_RES:
        candidates += [s["masterUrl"] for s in streams if short(s) == MAX_RES]
    # 2순위: 업로드 원본 파일 (MAX_RES보다 크면 받은 뒤 줄임)
    key = video.get("consumer", {}).get("originVideoKey")
    if key:
        candidates.append(f"https://sns-video-bd.xhscdn.com/{key}")
    # 3순위: 나머지 스트림 중 최고
    candidates += [s["masterUrl"] for s in streams]

    for u in candidates:
        try:
            download(u, out, ref, video=True)
            return [out], note_id, title
        except requests.HTTPError:
            continue
    raise RuntimeError("다운로드 가능한 영상 주소를 찾지 못함")


# ---------------- 작업 큐 ----------------
def run_job(n, url):
    """결과: ("ok", (폴더, 임시파일들, 영상ID, 제목)) / ("skip", "") / ("fail", 실패 이유)"""
    _job.tag, _job.n = f"[{n}]", n
    folder = site_folder(url)
    err = ""
    for attempt in range(1, RETRIES + 1):
        try:
            if folder is None:
                raise Permanent("지원하지 않는 사이트 (더우인/샤오홍슈만 지원)")
            res = douyin(url) if folder == "douyin" else xhs(url)
            if res == "skip":
                return "skip", ""
            return "ok", (folder, *res)
        except Permanent as e:
            err = str(e)
            break
        except requests.ConnectionError:
            err = "인터넷 연결 오류 (사이트 접속 실패)"
        except requests.Timeout:
            err = "응답 시간 초과"
        except Exception as e:
            err = str(e).splitlines()[0][:150] if str(e) else type(e).__name__
        if attempt < RETRIES:
            wait = 5 * attempt
            log(f"실패 ({attempt}/{RETRIES}): {err} -> {wait}초 뒤 다시 시도")
            time.sleep(wait)
    log(f"!! 최종 실패: {err}")
    return "fail", err


class Queue:
    """다운로드는 동시에, 저장(번호 붙이기)은 붙여넣은 순서대로 하나씩 -> 번호가 빠짐없이 순서대로"""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=WORKERS)
        self.jobs = []  # (작업번호, url, future)
        self.results = []  # (작업번호, url, 상태, 실패 이유)
        self.seen = set()
        self.cond = threading.Condition()
        self.closing = False
        self.saver = threading.Thread(target=self._save_loop, daemon=True)
        self.saver.start()

    def add(self, text):
        urls = URL_RE.findall(text)
        if not urls and text.strip():
            log("URL을 찾지 못했어요.")
        for url in urls:
            if url in self.seen:
                log(f"이미 추가된 링크라 건너뜀: {url}")
                continue
            self.seen.add(url)
            with self.cond:
                n = len(self.jobs) + 1
                log(f"[{n}] 추가됨: {url}")
                self.jobs.append((n, url, self.pool.submit(run_job, n, url)))
                self.cond.notify()

    def pending(self):
        return len(self.jobs) - len(self.results)

    def _save_loop(self):
        i = 0
        while True:
            with self.cond:
                while i >= len(self.jobs) and not self.closing:
                    self.cond.wait()
                if i >= len(self.jobs):
                    return
                n, url, fut = self.jobs[i]
            status, data = fut.result()
            if status == "ok":
                status, data = self._save(n, url, *data)
            self.results.append((n, url, status, data))
            i += 1

    def _save(self, n, url, folder, files, item_id, title):
        _job.tag, _job.url = f"[{n}]", url
        try:
            if have := already_have(folder, item_id):  # 다른 링크로 같은 영상을 넣은 경우
                log(f"이미 받은 영상이라 건너뜀: {have}")
                return "skip", ""
            num = next_number(folder)
            (SAVE_DIR / folder).mkdir(exist_ok=True)
            names = []
            for i, f in enumerate(files, 1):
                names.append(f"{num}{f.suffix}" if len(files) == 1 else f"{num}_{i}{f.suffix}")
                os.replace(f, SAVE_DIR / folder / names[-1])
            label = names[0] if len(names) == 1 else f"{num}_1~{len(names)}{files[0].suffix}"
            record(folder, label, item_id, title)
            log(f"저장됨: {folder}\\{label}  ({title[:30]})")
            return "ok", ""
        except Exception as e:
            log(f"!! 저장 실패: {e}")
            return "fail", f"저장 실패: {e}"
        finally:
            for f in files:
                f.unlink(missing_ok=True)

    def finish(self):
        if self.pending():
            log(f"\n남은 {self.pending()}개가 끝날 때까지 기다리는 중...")
        self.pool.shutdown(wait=True)
        with self.cond:
            self.closing = True
            self.cond.notify()
        self.saver.join()
        results = self.results
        ok = sum(r[2] == "ok" for r in results)
        skip = sum(r[2] == "skip" for r in results)
        failed = [r for r in results if r[2] == "fail"]
        line = "=" * 50
        log(f"\n{line}\n완료: 성공 {ok}개 / 이미 있어서 건너뜀 {skip}개 / 실패 {len(failed)}개")
        if failed:
            log(f"\n!! {len(failed)}개는 받지 못했어요 (최대 {RETRIES}번 시도):")
            for n, u, _, err in failed:
                log(f"  [{n}] {u}\n       이유: {err}")
            FAILED_FILE.write_text("\n".join(u for _, u, _, _ in failed) + "\n", encoding="utf-8")
            log(f"\n실패한 링크를 {FAILED_FILE.name} 에 저장했어요.")
            log(f"나중에 {FAILED_FILE.name} 파일을 영상다운로드.bat 위에 끌어다 놓으면 다시 시도합니다.")
            print("\a", end="", flush=True)  # 알림음
        else:
            FAILED_FILE.unlink(missing_ok=True)
        log(line)


def main():
    # 한글 윈도우 콘솔(cp949)에서 이모지 제목 출력 시 죽지 않도록
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    # 지난번에 중간에 끈 경우 남은 임시 파일 정리, 폴더는 숨김 처리
    if TMP_DIR.exists():
        for p in TMP_DIR.iterdir():
            p.unlink(missing_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    if os.name == "nt":
        subprocess.run(["attrib", "+h", str(TMP_DIR)], capture_output=True)
    q = Queue()
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.lower().endswith(".txt") and Path(arg).is_file():
                q.add(Path(arg).read_text(encoding="utf-8", errors="replace"))
            else:
                q.add(arg)
        q.finish()
        return
    print(f"저장 위치: {SAVE_DIR}   (동시에 {WORKERS}개씩, 실패 시 {RETRIES}번까지 재시도)")
    print("더우인/샤오홍슈 링크를 붙여넣고 Enter. 받는 동안에도 계속 붙여넣으면 됩니다.")
    print("여러 줄 한 번에 붙여넣기도 OK. 빈 줄 Enter: 진행 중이면 상황 표시, 다 끝났으면 종료.\n")
    try:
        while True:
            line = input().strip()
            if line:
                q.add(line)
            elif q.pending():
                log(f"진행 중 {q.pending()}개 (끝나고 빈 줄 Enter 치면 종료)")
            else:
                break
    except (EOFError, KeyboardInterrupt):
        pass
    q.finish()


if __name__ == "__main__":
    main()
