"""端到端验证：两轮复习之间，薄弱点通过 PowerContext 跨会话复用。

运行前先启动两个服务：
    powercontext server run
    python app.py
然后执行：
    python scripts/verify_demo.py
"""

from __future__ import annotations

import sys
import time

import httpx

APP = "http://127.0.0.1:7860"
PC = "http://127.0.0.1:8000"
LEARNER = "验证样例用户"


def wait_ready(client: httpx.Client, url: str, name: str) -> None:
    for _ in range(20):
        try:
            if client.get(url).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{name} 未就绪：{url}")


def start_session(client: httpx.Client) -> dict:
    response = client.post(
        f"{APP}/api/session/start", json={"learner": LEARNER, "weak_only": False}
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    with httpx.Client(timeout=30, trust_env=False) as client:
        wait_ready(client, f"{PC}/health/ready", "PowerContext")
        wait_ready(client, f"{APP}/api/health", "StudyLoop")

        boot = client.get(f"{APP}/api/bootstrap", params={"learner": LEARNER}).json()
        print(f"PowerContext scope: {boot['scope_id']}")
        print(f"验证前已有记忆: {boot['memory_count']} 条")

        print("\n[第一轮] 全部选 A，故意制造错题 …")
        session = start_session(client)
        answers = [
            {"id": question["id"], "choice": 0} for question in session["questions"]
        ]
        result = client.post(
            f"{APP}/api/session/submit",
            json={
                "learner": LEARNER,
                "session_id": session["session_id"],
                "answers": answers,
            },
        )
        result.raise_for_status()
        report = result.json()
        wrong = [item for item in report["details"] if not item["correct"]]
        print(f"得分: {report['score']}/{report['total']}，答错 {len(wrong)} 题")
        for item in wrong:
            print(f"  - 薄弱点: {item['topic']} / {item['question'][:28]}…")
        for write in report["writes"]:
            print(f"  写入 [{write['kind']}] {write['text'][:60]}… ({write['entry_id'][:20]})")

        print("\n[第二轮] 模拟新会话，检查记忆是否被召回 …")
        session_b = start_session(client)
        recalled = session_b["recalled"]
        weak_topics = session_b["weak_topics"]
        print(f"召回记忆 {len(recalled)} 条：")
        for item in recalled[:5]:
            print(f"  - [{item['kind']}] {item['text'][:50]}…")
        print(f"薄弱点统计: {weak_topics}")
        first_topic = session_b["questions"][0]["topic"]
        print(f"本轮第一题主题: {first_topic}")

        checks = {
            "第一轮产生了错题": len(wrong) > 0,
            "错题已写入 PowerContext": any(
                write["kind"] == "weakness" for write in report["writes"]
            ),
            "第二轮召回了记忆": len(recalled) > 0,
            "薄弱点已进入排题依据": len(weak_topics) > 0,
            "薄弱主题被优先安排": first_topic in weak_topics,
        }
        print("\n验证结果：")
        for name, ok in checks.items():
            print(f"  [{'通过' if ok else '失败'}] {name}")
        if all(checks.values()):
            print("\n验证通过：PowerContext 中的记忆确实被后续会话复用。")
            return 0
        print("\n验证失败：请检查上面的输出。")
        return 1


if __name__ == "__main__":
    sys.exit(main())