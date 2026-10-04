# 최종 평가셋

이 폴더는 에이전트 세션이 읽지 않는다. `.claude/settings.json`이 Read·Edit 모두 deny로 막고 있다.

- 사용자가 혼자 작성한다. 작성 중 규정 본문(`policy` 테이블)과 개발 평가셋을 보지 않는다.
- 딱 한 번 실행하고 결과를 `analysis/experiments.csv`에 남긴다.
- 점수가 나쁘면 그것이 결과다. 이 파일을 보고 프롬프트를 고치면 그 다음 숫자는 아무것도 증명하지 못한다.
- 형식은 `data/eval-dev/routes.jsonl`과 같다.