# lms_downloader.py
import sys
import re
import time
import unicodedata
import os, subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from PyQt5.QtCore import QProcess
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QPushButton, QFileDialog, QPlainTextEdit, QMessageBox, QCheckBox, QTextEdit
)

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, UnexpectedAlertPresentException, NoAlertPresentException

def open_folder(path: str):
    """OS별로 파일 탐색기 열기"""
    if sys.platform.startswith("darwin"):  # macOS
        subprocess.run(["open", path])
    elif os.name == "nt":  # Windows
        os.startfile(path)
    elif os.name == "posix":  # Linux
        subprocess.run(["xdg-open", path])

# ---------------------- 유틸 ----------------------
def extract_id_from_url(u: str) -> str:
    """?id= 숫자 뽑기 (없으면 도메인+타임스탬프)"""
    try:
        q = parse_qs(urlparse(u).query)
        if "id" in q and q["id"]:
            return q["id"][0]
    except Exception:
        pass
    ts = str(int(time.time()*1000))
    host = urlparse(u).netloc.replace(".", "_")
    return f"{host}_{ts}"


def get_base_url(full_url: str) -> str:
    """전체 URL에서 스킴+도메인까지만 추출 → https://domain/"""
    p = urlparse(full_url)
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}/"


def sanitize_filename(name: str, max_len: int = 150) -> str:
    """
    파일 시스템에 안전한 파일명 생성 + 글자 사이 공백 보정
    """
    # 1) 정규화 & 트림
    name = unicodedata.normalize("NFKC", name or "").strip()

    # 2) 금지문자 제거
    forbidden = '<>:"/\\|?*\0'
    name = "".join("" if ch in forbidden else ch for ch in name)

    # 3) 라틴 문자 사이 공백 제거 (L e c t u r e -> Lecture)
    name = re.sub(r'(?<=[A-Za-z])\s+(?=[A-Za-z])', '', name)

    # 4) 여러 종류의 공백을 한 칸으로
    name = re.sub(r'\s+', ' ', name)

    # 5) 구두점/하이픈 주변 정돈
    #   - 쉼표/닫는 괄호 앞 공백 제거: " ,", " ]" 등
    name = re.sub(r'\s+,', ',', name)
    name = re.sub(r'\s+([\)\]\}])', r'\1', name)
    #   - 하이픈 주변은 양쪽 한 칸: " - "
    name = re.sub(r'\s*-\s*', ' - ', name)
    #   - 여는 괄호/대괄호 뒤 공백 제거
    name = re.sub(r'([\(\[\{])\s+', r'\1', name)

    # 6) 남은 제어문자 치환
    name = "".join(ch if (ch.isprintable()) else " " for ch in name).strip()

    # 7) 길이 제한
    if not name:
        name = "untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip()

    return name


def build_cookie_header_from_driver(driver, target_url: str) -> str:
    """
    Selenium driver에 저장된 쿠키를 가져와 ffmpeg에 넣을 Cookie 헤더 문자열로 변환.
    target_url 도메인과 매칭되는 쿠키만 포함.
    """
    try:
        cookies = driver.get_cookies()
    except Exception:
        return ""

    t_host = urlparse(target_url).netloc
    pairs = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        domain = (c.get("domain") or "").lstrip(".")
        if name and value and (t_host.endswith(domain) or domain.endswith(t_host)):
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


# ---------------------- 메인 GUI ----------------------
class HlsDownloader(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LMS Downloader")
        self.setMinimumWidth(920)

        self.proc = None            # 현재 실행 중인 ffmpeg QProcess
        self.driver = None          # Selenium driver (로그인 세션 유지)
        self.pending_jobs = []      # (page_url, m3u8_url, out_file, referer)
        self.current_job = None

        # --- URL들 입력 (여러 줄)
        self.urls_edit = QTextEdit()
        self.urls_edit.setPlaceholderText(
            "https://ys.learnus.org/mod/vod/viewer.php?id=4110793\n"
            "https://plms.postech.ac.kr/mod/vod/viewer.php?id=196921"
        )

        # --- 출력 폴더
        self.out_dir_edit = QLineEdit(str(Path.home() / "Documents"))
        btn_dir = QPushButton("저장 폴더...")
        btn_dir.clicked.connect(self.choose_out_dir)

        # --- 옵션 (User-Agent, -c copy)
        self.ua_edit = QLineEdit(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
        )
        self.chk_copy = QCheckBox("재인코딩 없이 저장(-c copy)")
        self.chk_copy.setChecked(True)

        # --- 제어 버튼
        self.btn_login = QPushButton("로그인 시작(브라우저 열기)")
        self.btn_fetch = QPushButton("추출+다운로드 시작")
        self.btn_stop = QPushButton("현재 항목 중지")
        self.btn_close_browser = QPushButton("브라우저 닫기")

        self.btn_fetch.setEnabled(False)        # 로그인 세션 준비 전에는 비활성화
        self.btn_stop.setEnabled(False)

        self.btn_login.clicked.connect(self.start_browser_and_login)
        self.btn_fetch.clicked.connect(self.start_batch)
        self.btn_stop.clicked.connect(self.stop_current)
        self.btn_close_browser.clicked.connect(self.close_browser)

        # --- 로그창
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)

        # --- 레이아웃
        root = QVBoxLayout()

        row = QHBoxLayout()
        row.addWidget(QLabel("LMS 강의 URL들(줄바꿈 구분):"))
        root.addLayout(row)
        root.addWidget(self.urls_edit)

        row = QHBoxLayout()
        row.addWidget(QLabel("저장 폴더:"))
        row.addWidget(self.out_dir_edit, 1)
        row.addWidget(btn_dir)
        root.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("User-Agent:"))
        row.addWidget(self.ua_edit)
        root.addLayout(row)

        root.addWidget(self.chk_copy)

        row = QHBoxLayout()
        row.addWidget(self.btn_login)
        row.addWidget(self.btn_fetch)
        row.addWidget(self.btn_stop)
        row.addWidget(self.btn_close_browser)
        root.addLayout(row)

        root.addWidget(QLabel("로그"))
        root.addWidget(self.log, 1)

        self.setLayout(root)

    # ---------- 공용 ----------
    def append_log(self, text: str):
        self.log.moveCursor(self.log.textCursor().End)
        self.log.insertPlainText(text)
        self.log.moveCursor(self.log.textCursor().End)

    # ---------- 브라우저/로그인 ----------
    def start_browser_and_login(self):
        """Selenium 크롬을 띄우고 사용자가 직접 로그인할 수 있게 함."""
        if self.driver:
            self.append_log("[INFO] 이미 브라우저가 열려 있습니다.\n")
            self.btn_fetch.setEnabled(True)
            return

        # URL 입력 칸에서 첫 줄을 가져와 base 도메인 산출
        urls = [u.strip() for u in self.urls_edit.toPlainText().splitlines() if u.strip()]
        if not urls:
            QMessageBox.warning(self, "입력 필요", "먼저 상단에 LMS 강의 URL(최소 1개)을 입력하세요.")
            return
        first_url = urls[0]
        start_url = get_base_url(first_url)
        if not start_url:
            QMessageBox.warning(self, "URL 오류", "유효한 URL이 아닙니다.")
            return

        self.append_log(f"[INFO] 크롬 브라우저 시작... (로그인 페이지: {start_url})\n")
        options = Options()
        # 로그인 시 창이 보여야 하므로 headless 금지
        # 장기 세션 유지 원하면 아래 주석 해제: 다음 실행에도 로그인 유지됨(개인 PC 권장)
        # options.add_argument(f"--user-data-dir={Path.cwd() / 'chrome_profile'}")
        driver = webdriver.Chrome(options=options)

        # 도메인 루트로 이동 → 로그인 유도
        driver.get(start_url)

        QMessageBox.information(
            self, "로그인 안내",
            "열린 브라우저에서 LMS 로그인을 완료하세요.\n"
            "로그인 완료 후 이 창으로 돌아와 ‘추출+다운로드 시작’을 눌러 주세요."
        )

        self.driver = driver
        self.btn_fetch.setEnabled(True)
        self.append_log("[OK] 로그인 세션 준비 완료. 이제 '추출+다운로드 시작'을 누르세요.\n")

    def close_browser(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self.append_log("[INFO] 브라우저 종료.\n")

    # ---------- 추출 + 다운로드 ----------
    def start_batch(self):
        if not self.driver:
            QMessageBox.warning(self, "로그인 필요", "먼저 '로그인 시작'으로 브라우저를 열고 로그인하세요.")
            return

        urls = [u.strip() for u in self.urls_edit.toPlainText().splitlines() if u.strip()]
        if not urls:
            QMessageBox.warning(self, "입력 필요", "LMS 강의 URL을 한 줄에 하나씩 입력하세요.")
            return

        out_dir = Path(self.out_dir_edit.text().strip() or ".").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        # 큐 초기화
        self.pending_jobs.clear()
        self.current_job = None

        # 각 URL에서 m3u8/제목을 추출해 큐에 넣음 (순차)
        existing_outputs = set()
        for page_url in urls:
            try:
                m3u8, page_title = self.extract_m3u8_and_title_from_page(page_url)
                if not m3u8:
                    self.append_log(f"[WARN] m3u8 추출 실패: {page_url}\n")
                    continue

                # 파일명: 제목 → 안전화 → 중복 방지
                vid = extract_id_from_url(page_url)
                base = sanitize_filename(page_title) if page_title else f"lms_{vid}"

                candidate = base
                suffix = 1
                while True:
                    out_path = out_dir / f"{candidate}.mp4"
                    out_str = str(out_path)
                    if (not out_path.exists()) and (out_str not in existing_outputs):
                        break
                    suffix += 1
                    candidate = f"{base} ({suffix})"

                out_file = str(out_dir / f"{candidate}.mp4")
                existing_outputs.add(out_file)

                referer = page_url  # 각 페이지를 참조 리퍼러로 사용
                self.pending_jobs.append((page_url, m3u8, out_file, referer))
                self.append_log(
                    f"[OK] 추출: {page_url}\n"
                    f"     제목: {page_title or '(없음)'}\n"
                    f"     파일: {out_file}\n"
                    f"     m3u8: {m3u8}\n"
                )
            except Exception as e:
                self.append_log(f"[ERROR] 추출 중 오류: {page_url} | {e}\n")

        if not self.pending_jobs:
            self.append_log("[DONE] 모든 다운로드 완료.\n")
            self.btn_stop.setEnabled(False)

            # 탐색기 열기
            out_dir = Path(self.out_dir_edit.text().strip() or ".").resolve()
            try:
                open_folder(str(out_dir))
                self.append_log(f"[INFO] 탐색기 열기: {out_dir}\n")
            except Exception as e:
                self.append_log(f"[WARN] 탐색기 열기 실패: {e}\n")

            return

        self.append_log(f"[INFO] 총 {len(self.pending_jobs)}개 항목 다운로드 시작...\n")
        self.run_next_job()

    def extract_m3u8_and_title_from_page(self, page_url: str):
        self.driver.get(page_url)

        # 팝업(이전 재생기록) 자동 처리
        try:
            time.sleep(1)  # 페이지 진입 직후 alert 뜰 시간
            alert = self.driver.switch_to.alert
            text = alert.text
            self.append_log(f"[INFO] 알림 발견: {text}\n → 자동 '확인' 클릭\n")
            alert.accept()   # 확인(=이어보기)
        except NoAlertPresentException:
            pass
        except UnexpectedAlertPresentException:
            try:
                alert = self.driver.switch_to.alert
                text = alert.text
                self.append_log(f"[INFO] 알림(예외) 발견: {text}\n → 자동 '확인' 클릭\n")
                alert.accept()
            except Exception:
                pass

        # <video> 대기
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "video"))
            )
        except Exception:
            pass

        # 제목 추출
        title = self.extract_title_from_page()

        # <video><source> 직접 추출
        try:
            elem = self.driver.find_element(By.CSS_SELECTOR, "video source")
            src = elem.get_attribute("src") or ""
            if ".m3u8" in src:
                return src, title
        except Exception:
            pass

        # 정규식 백업
        html = self.driver.page_source or ""
        m = re.search(r'https?://[^\s"\']+?\.m3u8[^\s"\']*', html)
        src = m.group(0) if m else ""
        return src, title
    
    def extract_title_from_page(self) -> str:
        """현재 페이지에서 <h1 class='vod-title'> 텍스트 추출 (없으면 빈 문자열)"""
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, "h1.vod-title")
            return (el.text or "").strip()
        except NoSuchElementException:
            return ""

    # ---------- ffmpeg 실행 ----------
    def run_next_job(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            return  # 현재 작업이 끝나길 기다림

        if not self.pending_jobs:
            self.append_log("[DONE] 모든 다운로드 완료.\n")
            self.btn_stop.setEnabled(False)
            return

        self.current_job = self.pending_jobs.pop(0)
        page_url, m3u8, out_file, referer = self.current_job

        if not self.is_ffmpeg_available():
            QMessageBox.critical(self, "ffmpeg 미설치", "ffmpeg 실행 파일을 찾을 수 없습니다.")
            self.pending_jobs.clear()
            return

        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "info",
            "-stats",
            # 네트워크 안정 옵션
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_at_eof", "1",
            "-rw_timeout", "20000000",
            "-timeout", "20000000",
        ]

        # 헤더 (Referer + UA + Cookie[선택])
        headers = []
        ua = self.ua_edit.text().strip()
        if referer:
            headers.append(f"Referer: {referer}")
        if ua:
            headers.append(f"User-Agent: {ua}")

        # m3u8 접근에 세션 쿠키가 필요한 경우 대비
        if self.driver:
            cookie_header = build_cookie_header_from_driver(self.driver, m3u8)
            if cookie_header:
                headers.append(f"Cookie: {cookie_header}")

        if headers:
            cmd += ["-headers", "\\r\\n".join(headers)]

        cmd += ["-i", m3u8]

        if self.chk_copy.isChecked():
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]

        cmd += [out_file]

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.on_read_output)
        self.proc.readyReadStandardError.connect(self.on_read_output)
        self.proc.finished.connect(self.on_finished_one)

        self.append_log(f"[RUN] {page_url}\n      → {out_file}\n      ffmpeg: {' '.join(cmd)}\n")
        self.btn_stop.setEnabled(True)
        self.proc.start(cmd[0], cmd[1:])

        if not self.proc.waitForStarted(3000):
            self.append_log("[ERROR] ffmpeg 시작 실패\n")
            self.btn_stop.setEnabled(False)
            self.run_next_job()  # 다음 작업 시도

    def on_read_output(self):
        if not self.proc:
            return
        out = bytes(self.proc.readAllStandardOutput()).decode(errors="ignore")
        if out:
            self.append_log(out)
        err = bytes(self.proc.readAllStandardError()).decode(errors="ignore")
        if err:
            self.append_log(err)

    def on_finished_one(self, code, status):
        page_url, m3u8, out_file, _ = self.current_job or ("", "", "", "")
        self.append_log(f"\n[INFO] 완료(code={code}): {out_file}\n\n")
        self.btn_stop.setEnabled(False)
        self.current_job = None
        self.run_next_job()

    def stop_current(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.append_log("\n[INFO] 현재 작업 중지...\n")
            self.proc.kill()
            self.proc.waitForFinished(2000)
            self.btn_stop.setEnabled(False)

    # ---------- 기타 ----------
    def choose_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "저장 폴더 선택", self.out_dir_edit.text())
        if d:
            self.out_dir_edit.setText(d)

    @staticmethod
    def is_ffmpeg_available() -> bool:
        from shutil import which
        return which("ffmpeg") is not None


# ---------------------- 엔트리포인트 ----------------------
def main():
    app = QApplication(sys.argv)
    w = HlsDownloader()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
