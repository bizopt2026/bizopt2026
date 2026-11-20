# 서버 PC 이관 작업 지시서 (Claude Code용)

이 문서는 **서버 PC에 설치된 Claude Code가 읽고 그대로 실행하는 작업 지시서**입니다. 솔루션 박스(이 저장소)를 사용자의 로컬 PC(예: 10.231.0.5)에서 서버 PC(예: 10.231.1.254)로 옮깁니다. 먼저 저장소 루트의 `CLAUDE.md` 와 `docs/운영가이드.md` 를 읽으세요.

규칙:
- 단계 순서대로 진행합니다. 각 단계는 ① 명령 ② 성공으로 볼 출력 ③ 실패 시 볼 곳으로 되어 있습니다. 성공 출력이 확인되기 전에는 다음 단계로 넘어가지 않습니다.
- 각 명령은 PowerShell 에서 **하나씩** 실행하고 출력을 확인합니다.
- 주소·포트·경로는 아래 `$` 변수 자리에 사람과 정한 값을 넣습니다. 이 문서의 주소(10.231.0.5, 10.231.1.254)와 포트 8443은 예시입니다.
- **스크립트 실행 정책:** Windows 10/11 클라이언트는 기본이 Restricted 라서 `.ps1` 을 그냥 실행할 수 없습니다. 이 문서의 스크립트는 모두 `powershell -ExecutionPolicy Bypass -File .\scripts\<이름>.ps1 <인자>` 로 실행합니다 (그 실행에만 적용되고 PC 설정은 바꾸지 않습니다).
- `scripts\*.ps1` 3개는 Linux에서 작성되어 **Windows 실행 검증이 안 된 상태**입니다. 실행 전에 내용을 읽고, 실패하면 고쳐서 쓰고, **고친 내용을 저장소에 반영**(커밋하고 사람에게 알림)하세요.

## 0. 사람에게 먼저 물을 것 (아무것도 바꾸기 전에)

다음을 사람에게 묻고 답을 받은 뒤에만 시작합니다. 묻기 전에 아래 확인 명령부터 실행해 결과를 보여 주세요.

```powershell
Get-NetTCPConnection -LocalPort 443 -ErrorAction SilentlyContinue | Select-Object LocalAddress, State, OwningProcess
```

```powershell
Get-NetTCPConnection -LocalPort 8443 -ErrorAction SilentlyContinue | Select-Object LocalAddress, State, OwningProcess
```

(출력이 없으면 그 포트를 쓰는 프로그램이 없다는 뜻입니다. 있으면 `Get-Process -Id <OwningProcess>` 로 프로그램 이름을 함께 보여 주세요.)

질문:
1. **접속 주소와 포트:** 직원이 접속할 서버 IP(또는 이름)와 포트. 기본은 8443, 443을 쓰려면 위 결과를 보여 주고 사람이 결정하게 합니다. → `$ServerHost`, `$Port`
2. **저장소 브랜치:** 받을 브랜치 이름. 사람이 말하지 않으면 `main` 이 있는지 `git ls-remote --heads` 로 확인해 보여 주고 고르게 합니다. → `$Branch` (저장소: https://github.com/bizopt2026/bizopt2026)
3. **설치 폴더:** 저장소를 받을 폴더, 예 `C:\bizopt2026`. → `$Clone`. 이 저장소는 앱마다 폴더를 나눠 관리하며 솔루션 박스는 그 안의 `solbox` 폴더다. → `$Repo` (= `$Clone\solbox`). 이후 모든 명령은 `$Repo` 에서 실행한다.
4. **서버 PC 자동 로그온:** 재부팅 뒤 사람이 로그온하지 않아도 서버가 켜지게 자동 로그온을 설정해도 되는지(회사 보안 정책상 가능한지).
5. **기존 데이터:** 로컬 PC(예: 10.231.0.5)에서 쓰던 `data` 폴더를 옮길지, 옮긴다면 어떻게 전달받을지(공유 폴더, USB 등)와 로컬 서버를 멈춰도 되는 시각.
6. **백업 위치:** 백업을 둘 폴더(가능하면 다른 드라이브나 공유 폴더). → `$BackupDir`
7. **Claude CLI 로그인:** 서버를 실행할 Windows 계정으로 이미 로그인돼 있는지.

답을 요약해 사람에게 되읽어 주고 "이대로 진행해도 됩니까?" 확인을 받으세요.

## 하지 말 것

- 기존 `data` 폴더를 **백업 없이** 삭제하거나 덮어쓰지 않는다 (먼저 폴더 이름을 바꿔 둔다). 이미 있는 `data` 안으로 다른 `data` 를 복사해 섞지 않는다.
- 이미 있는 설치 폴더(저장소 복제본)를 삭제하지 않는다. `app.db-wal`, `app.db-shm` 파일을 지우지 않는다.
- 비밀번호를 명령줄 인자(`--password`)나 파일에 적지 않는다.
- `secret_key`, 비밀번호, `server.key`, `config.toml` 을 저장소에 커밋하거나 채팅에 그대로 출력하지 않는다.
- **사람 확인 없이** 443 포트(또는 정한 포트)를 쓰는 다른 프로그램을 중지하지 않는다.
- 사람이 정하지 않은 주소·포트를 임의로 정하지 않는다.
- 방화벽을 "모두 허용"으로 열거나 기존 방화벽 규칙을 지우지 않는다 (`SolutionBox` 규칙만 다룬다).
- 로컬 PC의 서버를 사람 확인 없이 멈추거나 로컬의 `data` 를 지우지 않는다.
- 테스트나 점검을 건너뛰고 "됐다"고 보고하지 않는다.

## 변수 설정 (0단계의 답을 받은 직후)

아래 명령 블록들은 `$ServerHost`, `$Port`, `$Branch`, `$Clone`, `$Repo`, `$BackupDir` 변수를 씁니다. **모든 명령 블록은 같은 PowerShell 세션에서 실행**하세요 (세션을 새로 열었다면 이 블록을 다시 실행하거나, 값을 명령에 직접 적어 넣습니다). 값은 0단계에서 사람과 정한 것으로 바꿉니다. 아래는 예시입니다.

```powershell
$ServerHost = "10.231.1.254"; $Port = 8443; $Branch = "main"; $Clone = "C:\bizopt2026"; $Repo = "$Clone\solbox"; $BackupDir = "D:\solbox-backups"
```

- 성공: 오류 없이 끝난다. `Write-Host "$ServerHost : $Port"` 로 값이 맞는지 확인한다.

## 1. 준비물 확인

```powershell
python --version
```
- 성공: `Python 3.11` 이상 (3.11.x, 3.12.x …).
- 실패 시: Python 이 없거나 3.10 이하면 사람에게 설치를 요청한다 (설치 때 "Add python.exe to PATH").

```powershell
git --version
```
- 성공: `git version ...`. 실패 시: 사람에게 Git 설치를 요청하거나 zip 받기로 바꾼다.

```powershell
claude --version
```
- 성공: 버전 숫자가 출력된다. 실패 시: Claude CLI 설치 안내(https://docs.claude.com 의 Claude Code 설치)를 사람에게 전한다.

```powershell
claude -p "확인이라고만 답하세요"
```
- 성공: `확인` 비슷한 답이 출력된다 (이 계정에서 Claude 로그인이 돼 있다는 뜻).
- 실패 시: 로그인 오류면 사람에게 이 계정에서 `claude` 를 실행해 로그인하도록 요청한다.

## 2. 코드 받기와 설치

먼저 `$Repo` 폴더가 이미 있는지 확인한다 (Claude Code 가 이미 저장소 복제본 안에서 실행 중일 수 있다).

```powershell
Test-Path "$Clone\.git"
```
- `False` 면 새로 받는다:

```powershell
git clone -b $Branch https://github.com/bizopt2026/bizopt2026 $Clone
```
- 성공: `$Clone` 폴더와 그 안의 `solbox` 폴더(`$Repo`)가 생기고 오류 없이 끝난다. 실패 시: 네트워크·권한 오류 문구를 사람에게 보여 준다.
- `True`(이미 복제본이다) 면 폴더를 지우거나 다시 받지 말고 갱신한다. 작업 중인 변경이 있으면(`git status`) 사람에게 알린다.

```powershell
git -C $Clone fetch
```

```powershell
git -C $Clone checkout $Branch
```

```powershell
git -C $Clone pull
```
- 성공: 오류 없이 끝나고 `git -C $Clone status` 가 `$Branch` 브랜치를 가리킨다. 실패 시: 충돌·로컬 변경 문구를 사람에게 보여 준다 (임의로 `reset --hard` 하지 않는다).

```powershell
cd $Repo
```

```powershell
python -m pip install -e ".[dev]"
```
- 성공: `Successfully installed solbox-...` 로 끝난다. 실패 시: 오류 문구를 읽고 해당 패키지 설치 문제를 해결한다. 회사 프록시가 막으면 사람에게 알린다.

### 2-1. 음성 받아쓰기 (사람이 원할 때만, 선택)

받아쓰기를 쓰기로 한 경우에만 한다. **이 기능은 개발 환경에서 실제로 돌려 보지 못했으므로 여기서 처음 검증된다.** 결과를 그대로 사람에게 보고한다.

```powershell
python -m pip install -e ".[stt]"
```
- 성공: `faster-whisper` 가 설치된다. 실패 시: 오류 문구를 사람에게 보여 준다 (회사 프록시 등).

`config.toml` 작성(3번) 뒤에 `transcriber = "faster-whisper"` 와 `transcribe_model`(기본 small)을 적고, 서버를 멈춘 상태에서:

```powershell
python -m app.cli stt-setup
```
- 성공: 모델이 `data\models\` 에 받아지고 "자체 점검 통과". 인터넷 접속이 필요하다 (모델 저장소). 실패 시: 한국어 오류 문구를 사람에게 보여 준다. 인터넷이 막혔으면 임의로 우회하지 말고 사람에게 묻는다.

```powershell
python -m app.cli stt-test <사람이 준 녹음 파일 경로>
```
- 성공: 받아쓴 글, 걸린 시간, 속도가 나온다. 이 값을 사람에게 보고한다 (모델 크기를 정할 근거). 녹음 파일이 없으면 사람에게 요청한다.

## 3. config.toml 작성

먼저 `config.toml` 이 이미 있는지 확인한다 (이미 있는 복제본이면 있을 수 있다).

```powershell
Test-Path config.toml
```
- `True` 면 **복사하지 않는다** (덮어쓰면 `secret_key`·`data_dir` 를 잃는다). 키 이름만 보여 주고(값은 출력하지 않는다, 특히 `secret_key`) 사람에게 기존 파일을 유지할지 묻는다.

```powershell
Get-Content config.toml -Encoding UTF8 | Where-Object { $_ -match '^\s*[A-Za-z_]+\s*=' -and $_ -notmatch '^\s*secret_key' }
```
- 성공: `secret_key` 줄을 뺀 설정 줄이 보인다. 사람이 유지하겠다고 하면 아래 복사와 `secret_key` 새로 만들기는 건너뛰고 필요한 값만 고친다.
- `False` 면 복사한다:

```powershell
copy config.example.toml config.toml
```

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
- 성공: 43자 안팎의 무작위 문자열. 이 값을 `config.toml` 의 `secret_key` 에 넣는다 (채팅에 다시 출력하지 않는다).

`config.toml` 을 열어 다음 값으로 고친다 (Windows 경로는 `/` 로 쓴다).
- `host = "0.0.0.0"`, `port = $Port`
- `secret_key = "<위에서 만든 값>"`
- `data_dir = "$Repo/data"` (또는 사람이 정한 경로), `backup_dir = "$BackupDir"`
- 인증서 두 줄은 4단계 뒤에 넣는다.

```powershell
python -m app.cli check
```
- 성공: "설정 파일 읽기: 정상", "Python 버전: 정상", "Claude CLI: 정상" 등. 아직 인증서를 안 만들었으므로 "인증서" 줄은 없다. "포트: 정상" 이어야 한다.
- 실패 시: "문제: …" 줄의 안내대로 고친다. 포트가 사용 중이면 0단계의 사람과 상의한다 (프로그램을 멈추지 말고 포트를 바꾸는 쪽을 먼저 제안).

## 4. 인증서, 방화벽, 자동 시작

먼저 세 스크립트의 내용을 읽는다 (검증이 안 된 파일이다).

```powershell
Get-Content scripts\run_server.ps1, scripts\install_autostart.ps1, scripts\open_firewall.ps1 -Encoding UTF8
```
- 성공: 내용이 한글 깨짐 없이 보인다. 이상한 점이 있으면 실행 전에 고친다.

세 스크립트가 UTF-8 BOM으로 저장돼 있는지 확인한다 (BOM이 없으면 Windows PowerShell 5.1 이 한글을 잘못 읽어 오류가 난다).

```powershell
Get-ChildItem scripts\*.ps1 | ForEach-Object { "{0}: BOM={1}" -f $_.Name, (([IO.File]::ReadAllBytes($_.FullName)[0..2] -join ",") -eq "239,187,191") }
```
- 성공: 세 파일 모두 `BOM=True`.
- 실패 시: `BOM=False` 인 파일은 UTF-8 BOM으로 다시 저장한 뒤(`$t = Get-Content <파일> -Raw -Encoding UTF8; [IO.File]::WriteAllText("<절대경로>", $t, (New-Object Text.UTF8Encoding $true))`) 이 수정을 저장소에 반영한다.

인증서 만들기 (`$ServerHost` 는 0단계에서 정한 주소; 이름으로도 접속하면 `--host` 를 더 붙인다):

```powershell
python -m app.cli make-cert --host $ServerHost
```
- 성공: "인증서를 만들었습니다" 와 `ssl_certfile = "..."`, `ssl_keyfile = "..."` 두 줄. 이 두 줄을 `config.toml` 에 넣는다.
- 실패 시: "이미 있는 파일입니다" 면 기존 인증서의 주소가 맞는지 확인하고, 틀리면 `--overwrite` 를 붙인다.

방화벽 (**관리자 권한 PowerShell** 이 필요하다. 권한이 없으면 사람에게 관리자 PowerShell 을 열도록 요청한다. 관리자 PowerShell 은 **새 세션**이므로 먼저 `cd $Repo` 로 저장소 폴더에 가고, 위 "변수 설정" 블록을 다시 실행한다):

```powershell
cd $Repo
```


```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\open_firewall.ps1 -Port $Port
```
- 성공: "방화벽 규칙 'SolutionBox' 을 추가했습니다".
- 규칙을 지우려면 (관리자 PowerShell, 필요할 때만): `powershell -ExecutionPolicy Bypass -File .\scripts\open_firewall.ps1 -Port $Port -Remove`
- 확인: 아래 명령에 `SolutionBox` 규칙이 `Enabled: True` 로 나온다.

```powershell
Get-NetFirewallRule -DisplayName SolutionBox | Select-Object DisplayName, Enabled, Direction, Action
```
- 실패 시: 스크립트 오류 문구를 읽고 고친다. 규칙이 있는데 접속이 안 되면 `Get-NetConnectionProfile` 로 네트워크 프로필(공용 여부)을 사람에게 보여 준다.

자동 시작 등록 (이 스크립트는 로그온 때 시작하는 작업을 만든다). **서버를 실행할 Windows 계정으로**, 일반(관리자 아닌) PowerShell 에서 실행한다. 스크립트가 `$env:USERNAME` 계정을 작업 소유자로 등록하기 때문이다. 방화벽 단계의 관리자 창에서 실행하지 않는다 (다른 계정이 소유자가 될 수 있다).

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```
- 성공: "작업 'SolutionBox' 을 등록했습니다".
- "액세스가 거부되었습니다" 가 나오면 등록에 권한이 더 필요한 PC이다. **같은 계정으로** PowerShell 을 관리자 권한으로 새로 열고(`cd $Repo` 와 변수 블록을 다시 실행) 같은 명령을 다시 실행한다.
- 확인:

```powershell
Get-ScheduledTask -TaskName SolutionBox | Select-Object TaskName, State
```
- 성공: `SolutionBox` 가 보인다. 실패 시: 오류 문구를 읽고 스크립트를 고친다 (예: 가상환경이면 `-PythonExe` 로 python.exe 전체 경로를 넘긴다).

자동 로그온은 0단계에서 사람이 허용한 경우에만, `netplwiz` 로 사람이 직접 하도록 안내한다 (비밀번호는 사람이 입력한다).

## 5. 데이터 옮기기 (0단계에서 옮기기로 한 경우만)

1. **사람에게 로컬 PC(예: 10.231.0.5)의 서버를 멈추도록 요청**하고, 멈췄다고 확인받는다. 직접 실행했다면 실행 창에서 `Ctrl+C`, 자동 시작을 썼다면 (창이 숨겨져 `Ctrl+C` 가 안 된다) 사람에게 그 PC의 PowerShell 에서 아래 두 명령(포트는 그 PC의 `config.toml` 값)을 실행하고 결과를 알려 달라고 요청한다.

```powershell
Stop-ScheduledTask -TaskName SolutionBox
```

```powershell
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
```
- 성공: 아무것도 출력되지 않는다 (그 PC의 포트에서 서버가 내려갔다). 계속 나오면 `OwningProcess` 를 `Get-Process -Id` 로 확인해 사람에게 보이고, 사람이 확인한 뒤에만 종료한다.

   이 서버 PC에 자동 시작 작업이 이미 있어 서버가 돌고 있다면 같은 방법으로 멈춘다 (나중에 다시 시작은 `Start-ScheduledTask -TaskName SolutionBox`).

2. 서버 PC에 이미 `data` 가 있으면 **이름을 바꿔 둔다.** 지우거나 그 안에 복사하지 않는다. 이름을 바꾸면 `data` 가 없는 상태가 되어 두 설치가 섞이지 않고, 옛 `app.db-wal`·`app.db-shm` 이 새 `app.db` 옆에 남아 DB 를 망가뜨리는 일도 막는다.

```powershell
Rename-Item "$Repo\data" "data-before-migration-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
```
- 성공: `data-before-migration-<날짜시각>` 폴더가 생기고 `data` 는 없다. `data` 가 아직 없으면 이 명령은 건너뛴다. 실패 시(파일이 사용 중): 서버가 아직 돌고 있는지 확인한다.

3. 전달받은 로컬 PC의 `data` 폴더 전체를 `$Repo\data` 로 복사한다 (대상이 없으므로 폴더째 복사가 된다). `app.db`, `uploads`, `decks` 가 있는지 확인한다. `app.db-wal`, `app.db-shm` 이 있으면 `app.db` 와 **함께** 복사하고 지우지 않는다.

```powershell
Get-ChildItem "$Repo\data" -Force | Select-Object Name
```
- 성공: `app.db`, `uploads`, `decks` 가 보인다 (`app.db-wal`, `app.db-shm` 은 있었다면 같이 보인다). 실패 시: 복사 위치(폴더가 한 겹 더 들어갔는지)를 확인한다. `-wal`·`-shm` 파일이 있으면 로컬 서버가 완전히 멈췄는지 사람에게 재확인한다.

4. 사본 폴더를 DB 기준으로 다시 만든다.

```powershell
python -m app.cli rebuild-knowledge
```
- 성공: "knowledge 폴더를 다시 만들었습니다". 실패 시: 오류 문구와 `data\logs\app.log` 를 본다.

## 6. 검증

```powershell
python -m app.cli check
```
- 성공: 모든 줄이 "정상" (서버가 아직 안 떠 있어야 "포트: 정상"). 끝에 "모두 정상입니다." 실패 시: "문제:" 줄을 고친다.

```powershell
python -m pytest -q -W error
```
- (받아쓰기를 켠 경우 `check` 의 "음성 받아쓰기" 줄이 "정상 (faster-whisper, …, CPU)" 여야 한다.)
- 성공: 마지막 줄이 `N passed` 이고 failed·error·warning 이 없다. 실패 시: 실패한 테스트 이름과 메시지를 사람에게 보고하고 임의로 테스트를 고치지 않는다.

관리자 계정이 없으면 만든다 (옮긴 `data` 에 이미 있으면 건너뛴다). 이 명령은 비밀번호를 입력창(getpass)으로 두 번 받는데, 에이전트의 셸은 그 입력창에 답할 수 없다. **사람에게 자기 터미널(서버 PC의 PowerShell)에서 `cd $Repo` 후 아래 명령을 직접 실행하도록 요청한다.** `--password` 를 명령줄에 붙이거나 비밀번호를 파일에 적지 않는다.

```powershell
python -m app.cli create-admin --username admin --name 관리자
```
- 성공: "관리자 계정을 만들었습니다: admin". 사람이 끝났다고 알려 줄 때까지 기다린다.

서버를 시작한다 (자동 시작 작업으로):

```powershell
Start-ScheduledTask -TaskName SolutionBox
```

서버가 뜰 때까지 약 10초 기다린 뒤 확인한다.

```powershell
Start-Sleep -Seconds 10
```

```powershell
Get-NetTCPConnection -LocalPort $Port -State Listen
```
- 성공: `Listen` 상태가 한 줄 나온다. 실패 시: `Get-Content "$Repo\data\logs\server-console.log" -Tail 50 -Encoding UTF8` 와 `data\logs\app.log` 를 읽는다.

```powershell
Invoke-WebRequest -Uri "https://${ServerHost}:$Port/login" -SkipCertificateCheck -UseBasicParsing | Select-Object StatusCode
```
- (PowerShell 7 이상. Windows PowerShell 5.1 에는 `-SkipCertificateCheck` 가 없으므로 브라우저로 확인한다.)
- 성공: `StatusCode 200`. 이 점검은 자체 서명 인증서 때문에 인증서 검증만 건너뛴 것이다.

사람과 함께 확인한다 (사람이 브라우저로 직접):
- [ ] `https://$ServerHost:$Port` 접속과 로그인 (인증서 경고는 운영가이드 4번 안내)
- [ ] 관리 → 시스템 → "AI 연결 확인" 이 "AI 연결 정상" 으로 바뀐다. 실패하면 한국어 오류 문구를 그대로 기록한다.
- [ ] **다른 PC**에서 같은 주소로 접속된다. 안 되면 방화벽 규칙·`host = "0.0.0.0"`·네트워크 프로필을 확인한다.
- [ ] **재부팅 뒤 자동 시작:** 사람의 허락을 받고 재부팅한 뒤, (자동 로그온 설정이 있으면 로그온 없이, 없으면 로그온 뒤) 위 `Get-NetTCPConnection` 명령에 `Listen` 이 나오는지 확인한다. 안 나오면 `Get-ScheduledTaskInfo -TaskName SolutionBox` 의 `LastTaskResult` 와 `server-console.log` 를 본다.

백업:

```powershell
python -m app.cli backup
```
- 성공: "백업했습니다: <경로> (<크기>)". 출력된 경로를 아래에 넣는다.

```powershell
python -m app.cli backup --verify <위에서 출력된 백업 경로>
```
- 성공: "이상 없음". 실패 시: 출력된 문제 줄을 보고 `backup_dir` 권한과 디스크 여유를 확인한다.

## 7. 사람에게 보고할 것

아래 틀을 채워 보고한다 (비밀번호·`secret_key` 는 적지 않는다).

```
[서버 이관 보고]
- 접속 주소: https://<ServerHost>:<Port>
- 코드 브랜치와 커밋: <Branch> / <git rev-parse --short HEAD>
- 설치 폴더 / 데이터 폴더: <Repo> / <data_dir>
- 관리자 계정: <아이디> (비밀번호는 사람이 정한 것. 처음이면 변경 안내)
- 데이터 이전: 함, 파일 수 <N> / 안 함 / 기존 data 복사본 위치 <경로>
- 백업 위치: <BackupDir>, 최근 백업 <경로>, 검증 결과 <이상 없음>
- 자동 시작: 등록함, 재부팅 확인 <함/안 함>, 자동 로그온 <설정함/안 함>
- 방화벽 규칙 SolutionBox: <포트>
- 점검 결과: check <모두 정상/문제 내용>, pytest <N passed>, AI 연결 확인 <정상/오류 문구>, 다른 PC 접속 <확인/미확인>
- 스크립트를 고친 내용(저장소에 반영 여부): <없음/내용>
- 남은 문제와 사람이 할 일: <목록> (예: 직원 PC에 인증서 신뢰 등록, 로컬 PC의 옛 서버 종료·자동 시작 해제)
```
