# Windows 판 메모

엔진(`meridian/`)은 플랫폼을 가리지 않는다. OpenCV·SciPy·rawpy 모두 윈도우 휠이 있고,
운영체제에 기대는 부분(파일 선택창, 탐색기 열기, 부모 프로세스 감시)은 분기해 두었다.
화면만 플랫폼마다 다르다.

```
Meridian.exe        WinUI 3 화면 (C#)
engine\Meridian.exe 엔진 (PyInstaller 로 묶은 파이썬)
```

화면이 켜지면서 빈 포트를 하나 잡아 엔진을 자식 프로세스로 띄우고, 그 뒤로는 HTTP 로만
대화한다. 엔진에는 `MERIDIAN_ENGINE_ONLY=1`, `MERIDIAN_PORT`, `MERIDIAN_PARENT_PID` 를
넘긴다. 마지막 것 덕분에 화면이 강제 종료돼도 엔진이 고아로 남지 않는다.

## 빌드

[`.github/workflows/windows.yml`](../.github/workflows/windows.yml) 이 깃허브의 윈도우
러너에서 전부 만든다. 손으로 할 때는 PowerShell 에서:

```powershell
.\windows\build_windows.ps1                        # 엔진
dotnet publish winui\Meridian.WinUI.csproj -c Release -r win-x64 -o publish
New-Item -ItemType Directory -Force publish\engine
Copy-Item -Recurse -Force dist\Meridian\* publish\engine\
```

- .NET SDK 는 **8.x** 를 쓴다. 10.x 에서는 Windows App SDK 가 쓰는 패키징 작업
  (`ExpandPriContent`)을 찾지 못해 빌드가 깨진다. `global.json` 으로 못박아 두었다
- 엔진 빌드 결과가 150MB 보다 작으면 스크립트가 실패로 처리한다. PyInstaller 가
  라이브러리를 놓쳐도 exe 는 만들어지는데, 실행하면 곧바로 죽기 때문이다

## 확인해야 할 것들

맥에서 코드를 읽고 짚어 둔 것이거나, 실제로 겪은 것들이다. 윈도우에서 손볼 때 여기부터
보면 된다.

### 한글 경로

`cv2.imread` / `cv2.imwrite` 는 윈도우에서 경로에 비ASCII 문자가 있으면 오류 없이
`None` / `False` 를 돌려준다. 사용자 이름이나 폴더 이름에 한글이 흔하다.
`np.fromfile` + `cv2.imdecode`, `cv2.imencode` + `tofile` 로 감싸면 양쪽 플랫폼에서
똑같이 동작한다. **아직 손보지 않았다.**

### 캐시·프로젝트 저장 위치

엔진은 `server.py` 의 `ROOT` 아래에 `cache/`, `projects/` 를 만든다. PyInstaller 번들에서는
프로그램 폴더 안이 되어, Program Files 에 두면 쓰기가 막힌다. `%LOCALAPPDATA%\Meridian\`
으로 옮겨야 한다. **아직 손보지 않았다.**

캐시에는 사진 축소본과 기가픽셀 내보내기용 임시 캔버스(수십 GB 까지 간다)가 들어간다.

### 메모리 맵 파일 삭제

`render.render_final` 은 큰 캔버스를 `np.memmap` 으로 디스크에 잡았다가 끝나면 지운다.
윈도우는 매핑이 살아 있는 파일을 지우면 `PermissionError` 가 난다. 내보내기를 끝까지
해 보고, **중간에 취소도 해 보고** 임시 파일(`canvas_*.dat`, `.mask`)이 남는지 봐야 한다.

### 되돌리면 안 되는 것

- `server.py` 의 부모 프로세스 감시: 윈도우에서 `os.kill(pid, 0)` 은 존재 확인이 아니라
  **프로세스를 죽인다**. 그래서 `OpenProcess` 로 분기해 두었다
- `blend.py` 의 `SAFE_SEAM_FINDERS`: OpenCV 5 의 `VoronoiSeamFinder` / `NoSeamFinder` 는
  파이썬에서 부르면 프로세스가 통째로 죽는다(세그폴트). 막아 둔 것이다

### 배포

서명이 없는 exe 는 SmartScreen 과 Defender 가 경고할 수 있다. PyInstaller 실행 파일은
오탐도 잦다.

## 화면 기능 상태

`winui/` 는 맥 앱을 따라간다. 지금 있는 것과 없는 것:

| 기능 | 상태 |
|---|---|
| 사진 추가·목록, 자동 정렬·최적화·수평 | 있음 |
| 미리보기, 확대/맞춤, 갱신 버튼(설정 바꾼 뒤 그리기) | 있음 |
| 투영·리틀 플래닛/터널 프리셋 | 있음 |
| 파노라마 방향 (슬라이더·숫자·끌어서 돌리기·빠른 미리보기) | 있음 |
| 화각 (가로·세로·맞춤) | 있음 |
| 합성 설정, 렌즈 정보, 출력 크기·형식·품질, 내보내기(취소 포함) | 있음 |
| 제어점 편집기 | **없음** |
| 사진을 창에 끌어다 놓기 | **없음** |
| 사이드바에서 사진 빼기·기준 바꾸기 | **없음** |

맥 화면(`mac/Sources/*.swift`)이 사실상 명세다. 특히 아래 세 가지는 맥에서 실제로 겪고
고친 것이라, 새로 만들 때도 그대로 지키는 편이 좋다.

- 빠른 미리보기 요청은 **한 번에 하나만** 보낸다. 쌓으면 손은 멈췄는데 그림이 한참
  뒤따라온다
- 마지막으로 정한 방향은 **화면이 들고 있는다**. 서버 값은 렌더가 끝나야 따라오므로,
  그 사이에 다시 끌면 옛 방향에서 시작해 튄다
- 늦게 끝난 렌더 결과는 **버린다**. 그대로 올리면 새 방향으로 가 있던 화면이 뒤로 튄다
