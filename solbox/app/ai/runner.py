"""Claude CLI 실행기. 프롬프트는 표준 입력으로 넘기고, 모든 실패는 ClaudeError 로 돌려준다."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# 허용 도구 목록은 보안 경계다. FILE_TOOLS 나 WEB_TOOLS 에 도구를 더하면 경계가 넓어진다.
FILE_TOOLS = ("Read", "Glob", "Grep")
WEB_TOOLS = ("WebSearch", "WebFetch")
DEFAULT_TOOLS = FILE_TOOLS

# (종류, 키워드) — 위에서부터 먼저 맞는 것이 이긴다.
_CLASSIFY = [
    ("auth", ("login", "authenticat", "unauthorized", "401", "oauth", "token has expired", "expired")),
    ("limit", ("usage limit", "rate limit", "quota", "429", "limit reached", "hit your limit")),
    ("network", ("enotfound", "econnrefused", "network", "getaddrinfo", "timed out",
                "connection error", "fetch failed", "econnreset", "etimedout")),
]


class ClaudeError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return self.message


def assert_tool_separation(allowed_tools: tuple[str, ...] | list[str]) -> None:
    """도구 이름은 FILE_TOOLS 또는 WEB_TOOLS 와 정확히 같아야 하고, 두 종류를 섞을 수 없다."""
    if not isinstance(allowed_tools, (tuple, list)) or not all(isinstance(t, str) for t in allowed_tools):
        raise TypeError("allowed_tools 는 문자열의 tuple 또는 list 여야 합니다.")
    for name in allowed_tools:
        if name not in FILE_TOOLS and name not in WEB_TOOLS:
            raise ValueError(f"허용되지 않은 도구입니다: {name}")
    if any(t in WEB_TOOLS for t in allowed_tools) and any(t in FILE_TOOLS for t in allowed_tools):
        raise ValueError("웹 도구와 파일 도구를 함께 쓸 수 없습니다.")


def classify_error(text: str) -> str:
    lowered = text.lower()
    for kind, words in _CLASSIFY:
        if any(w in lowered for w in words):
            return kind
    return "unknown"


def _kill_tree(proc: subprocess.Popen) -> None:
    """프로세스와 가능한 한 그 자식까지 죽인다 (best effort)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False, timeout=10,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _reap(proc: subprocess.Popen) -> None:
    """죽인 프로세스를 회수한다. 손자 프로세스가 파이프를 쥐고 있어도 멈추지 않는다."""
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError):
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        proc.wait()


class ClaudeRunner:
    def __init__(self, cmd: list[str], cwd: Path):
        self.cmd = list(cmd)
        self.cwd = Path(cwd)

    def _spawn(
        self,
        output_args: list[str],
        allowed_tools: tuple[str, ...] | list[str],
        cwd: Path | None = None,
        file_scope: bool = False,
    ) -> subprocess.Popen:
        """공통 명령 구성과 프로세스 시작. 실패는 ClaudeError 로 돌려준다.

        `file_scope` 이면 파일 도구를 작업 폴더 안으로만 허용한다 (`Read(./**)` 같은 경로 한정 규칙). 이름만 쓴 `Read` 는
        어느 경로든 미리 허용하는 것이라 `../` 나 절대 경로로 다른 파일을 읽을 수 있다. 파일 도구만 쓸 수 있다."""
        assert_tool_separation(allowed_tools)
        if file_scope and any(t not in FILE_TOOLS for t in allowed_tools):
            raise ValueError("파일 범위 제한은 파일 도구에만 쓸 수 있습니다.")
        approved = [f"{t}(./**)" for t in allowed_tools] if file_scope else list(allowed_tools)
        work_dir = Path(cwd) if cwd is not None else self.cwd
        exe = shutil.which(self.cmd[0])
        if exe is None:
            raise ClaudeError("not_installed", f"Claude CLI를 찾을 수 없습니다: {self.cmd[0]}")
        argv = [exe, *self.cmd[1:], "-p", *output_args,
                "--allowedTools", ",".join(approved),
                "--tools", ",".join(allowed_tools), "--strict-mcp-config"]
        work_dir.mkdir(parents=True, exist_ok=True)
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(
                argv, cwd=work_dir, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **popen_kwargs,
            )
        except FileNotFoundError as e:
            raise ClaudeError("not_installed", str(e)) from e
        except OSError as e:
            raise ClaudeError("unknown", str(e)[-500:]) from e

    def run(
        self,
        prompt: str,
        *,
        allowed_tools: tuple[str, ...] | list[str] = DEFAULT_TOOLS,
        timeout_s: int = 600,
        cwd: Path | None = None,
        file_scope: bool = False,
    ) -> str:
        proc = self._spawn(["--output-format", "json"], allowed_tools, cwd, file_scope)

        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            _reap(proc)
            raise ClaudeError("timeout", f"{timeout_s}초 안에 끝나지 않았습니다.") from None
        except BaseException:
            _kill_tree(proc)
            _reap(proc)
            raise

        stdout, stderr = stdout or "", stderr or ""
        data = None
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            pass

        if proc.returncode == 0 and data is not None and not data.get("is_error"):
            result = data.get("result")
            if isinstance(result, str):
                return result

        raw = stdout + "\n" + stderr
        if data is not None and isinstance(data.get("result"), str):
            raw = data["result"] + "\n" + stderr
        raise ClaudeError(classify_error(raw), raw.strip()[-500:])

    def run_stream(
        self,
        prompt: str,
        *,
        on_text: Callable[[str], None],
        allowed_tools: tuple[str, ...] | list[str] = DEFAULT_TOOLS,
        timeout_s: int = 600,
        cwd: Path | None = None,
    ) -> str:
        """stream-json 으로 실행한다. 받은 텍스트를 누적해 on_text(누적)을 부르고 최종 결과 텍스트를 돌려준다."""
        proc = self._spawn(
            ["--output-format", "stream-json", "--verbose", "--include-partial-messages"], allowed_tools, cwd
        )
        stderr_parts: list[str] = []
        timed_out = False
        lines: queue.Queue = queue.Queue()
        deadline = time.monotonic() + timeout_s

        def write_prompt() -> None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.flush()
            except (OSError, ValueError):
                pass  # 프로세스가 입력을 다 읽기 전에 끝났다. 결과 분류는 종료 상태로 한다.
            finally:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

        def drain_stderr() -> None:
            try:
                for chunk in iter(lambda: proc.stderr.read(4096), ""):
                    stderr_parts.append(chunk)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stderr.close()  # 읽기 스레드가 끝나면서 스스로 닫는다
                except (OSError, ValueError):
                    pass

        def read_stdout() -> None:
            # 손자 프로세스가 파이프를 쥐고 있어도 본 루프가 마감 시각에 멈출 수 있게 줄을 큐로 넘긴다.
            try:
                for line in proc.stdout:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)
                try:
                    proc.stdout.close()
                except (OSError, ValueError):
                    pass

        writer = threading.Thread(target=write_prompt, name="claude-stdin", daemon=True)
        err_reader = threading.Thread(target=drain_stderr, name="claude-stderr", daemon=True)
        out_reader = threading.Thread(target=read_stdout, name="claude-stdout", daemon=True)
        threads = [writer, err_reader, out_reader]
        accumulated = ""
        result_line: dict | None = None
        try:
            for t in threads:
                t.start()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    line = lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = data.get("type")
                if kind == "result":
                    result_line = data
                elif kind == "stream_event":
                    event = data.get("event")
                    delta = event.get("delta") if isinstance(event, dict) else None
                    if (isinstance(event, dict) and event.get("type") == "content_block_delta"
                            and isinstance(delta, dict) and delta.get("type") == "text_delta"
                            and isinstance(delta.get("text"), str)):
                        accumulated += delta["text"]
                        try:
                            on_text(accumulated)
                        except Exception:  # noqa: BLE001 - 콜백 오류로 실행을 멈추지 않는다
                            log.exception("on_text 콜백 오류")
            if not timed_out:
                try:
                    proc.wait(timeout=max(deadline - time.monotonic(), 1.0))
                except subprocess.TimeoutExpired:
                    timed_out = True
        except BaseException:
            _kill_tree(proc)
            raise
        finally:
            if timed_out or proc.poll() is None:
                _kill_tree(proc)
            for t in threads:
                t.join(timeout=1 if timed_out else 5)
            if out_reader.is_alive():
                # 읽는 중인 파이프는 여기서 닫지 않는다. 손자가 끝나 읽기 스레드가 끝나면 스레드가 stdout·stderr 를 스스로 닫는다.
                proc.wait()
                if not writer.is_alive():
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
            else:
                _reap(proc)

        if timed_out:
            raise ClaudeError("timeout", f"{timeout_s}초 안에 끝나지 않았습니다.")

        stderr = "".join(stderr_parts)
        result = result_line.get("result") if result_line is not None else None
        if (proc.returncode == 0 and result_line is not None
                and not result_line.get("is_error") and isinstance(result, str)):
            return result
        raw = (result if isinstance(result, str) else "") + "\n" + stderr
        raise ClaudeError(classify_error(raw), raw.strip()[-500:] or "Claude 가 결과 없이 끝났습니다.")
