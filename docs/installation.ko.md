# Codex·Claude에서 Apple 미리 알림 쓰기

말로 할 일을 등록하고, 오늘 일정을 확인하고, 날짜를 바꾸거나 완료 처리하세요.

**macOS 14 이상 · Apple silicon·Intel 지원 · [v0.8.0 베타](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.8.0)**

## 설치

사용하는 앱을 펼쳐 보세요. 실행에 필요한 구성 요소는 모두 포함되어 있습니다.

<details>
<summary><strong>Codex</strong></summary>

Codex에 아래 명령을 실행해 달라고 하거나, `codex` 명령이 있는 터미널에서 실행하세요.

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.8.0
codex plugin add apple-reminders@oscar-v4-reminders
```

설치 후 **새 Codex 작업**을 여세요.

</details>

<details>
<summary><strong>Claude Code</strong></summary>

Claude Code 대화창에서 차례로 실행하세요.

```text
/plugin marketplace add https://github.com/Oscar-V4/apple-reminders.git#v0.8.0
/plugin install apple-reminders@oscar-v4-reminders
```

설치 후 Claude Code 세션을 다시 시작하세요.

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

1. [확장 파일 다운로드](https://github.com/Oscar-V4/apple-reminders/releases/download/v0.8.0/apple-reminders-0.8.0.mcpb)
2. **Settings → Extensions → Advanced settings → Install Extension…**에서 파일 선택
3. 확장을 활성화하고 새 대화 시작

</details>

## 이렇게 말해 보세요

- “이 회의록에서 할 일을 뽑아 미리 알림 목록으로 만들어 줘.”
- “이 프로젝트 계획을 바로 실행할 수 있는 작은 할 일로 나눠서 프로젝트 목록에 넣어 줘.”
- “이 두서없는 메모에서 해야 할 일만 골라 중복을 합치고 미리 알림 목록으로 정리해 줘.”

처음 요청할 때 macOS가 미리 알림 접근 권한을 물으면 허용하세요.

[문제 해결·업데이트·삭제](installation.md) · [지원 기능](workflow-capability-matrix.md)

---

독립 오픈소스 커뮤니티 플러그인입니다. 작업은 Mac에서 실행되며,
선택된 도구 결과는 사용 중인 AI 서비스로 전달됩니다. [개인정보 안내](../PRIVACY.md) · [라이선스](../LICENSE)
