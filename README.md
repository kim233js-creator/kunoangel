# KBO UNIVERSE 2.0 — Windows 패키징 준비판 v0.1.1

## 방법 A: Windows에서 EXE 빌드 (최초 1회 Python 필요)
1. Python 3.10 이상을 공식 사이트에서 설치하고 PATH에 추가합니다.
2. `build_windows_exe.bat`을 실행합니다. 인터넷 연결이 필요하며 PyInstaller를 설치합니다.
3. 성공하면 폴더에 `KBO_UNIVERSE.exe`가 생성됩니다.
4. 이후에는 Python 설치가 없는 Windows PC에서도 EXE를 실행할 수 있습니다.
5. 실행하면 브라우저가 `http://127.0.0.1:8765`로 자동 열립니다. 검은 창은 종료할 때까지 열어 두세요.

## 방법 B: 내 PC에 Python을 설치하지 않고 EXE 생성 (GitHub Actions)
1. GitHub에서 새 저장소를 생성합니다.
2. 이 폴더의 `app.py`와 `.github/workflows/build-windows.yml`을 같은 경로로 업로드합니다.
3. 저장소의 Actions > Build Windows EXE > Run workflow를 실행합니다.
4. 작업 완료 후 Artifacts의 `KBO_UNIVERSE_Windows`를 다운로드합니다.
5. 압축을 풀어 `KBO_UNIVERSE.exe`를 실행합니다.

**주의:** 이 ZIP에 미리 컴파일된 EXE는 들어 있지 않습니다. Windows 빌드가 필요합니다.

## 데이터 및 백업
- `universe.db`는 EXE 파일과 같은 폴더에 생성됩니다. 기존 세이브를 이어가려면 이전 폴더의 `universe.db`를 EXE와 같은 폴더에 복사하세요.
- 중요한 데이터는 프로그램의 전체 JSON 백업 기능으로 별도 저장하세요.
- Windows SmartScreen 경고가 발생할 수 있습니다. 본인이 직접 빌드한 파일인지 확인하세요.
- `start_windows.bat`은 EXE가 있으면 EXE를 우선 실행하며, 없으면 정상 Python 3 설치를 확인합니다.
- 포트 8765를 다른 프로그램이 사용 중이면 서버가 실행되지 않습니다.

## 현재 기능과 제한
세이브/경기/사건/로스터 기록, SQLite 저장, JSON 백업·복원, ChatGPT 인계용 Markdown. OOTP 23 CSV 자동 가져오기, 선수별 기록 입력 UI, 자동 순위, 기사 저장 UI는 아직 미구현입니다. 장기 시즌을 위해 정기적인 JSON 백업이 필요합니다.
