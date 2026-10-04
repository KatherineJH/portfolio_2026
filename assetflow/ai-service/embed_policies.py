"""규정을 임베딩해 policy_chunk 에 넣는다. 여러 번 실행해도 안전하다."""

from openai import OpenAI
from sqlalchemy import create_engine, text

from app.settings import settings

EMBED_MODEL = "text-embedding-3-small"

# situation_text 컬럼 추가
def main() -> None:
    client = OpenAI(api_key=settings.openai_api_key)
    engine = create_engine(settings.database_url)

    with engine.begin() as conn:
        policies = conn.execute(text(
            "SELECT id, policy_key, version, condition_text, exception_text, "
            "       required_info, allowed_action, situation_text  "
            "FROM policy ORDER BY id"
        )).mappings().all()

        # 다시 만들 때 중복되지 않도록 먼저 비운다.
        conn.execute(text("DELETE FROM policy_chunk"))

        total_chars = 0
        for policy in policies:
            parts = [
                f"규정 {policy['policy_key']} v{policy['version']}",
                f"조건: {policy['condition_text']}",
            ]
            if policy["situation_text"]:
                parts.append(f"적용되는 상황: {policy['situation_text']}")
            if policy["exception_text"]:
                parts.append(f"예외: {policy['exception_text']}")
            if policy["required_info"]:
                parts.append(f"필요 정보: {policy['required_info']}")
            parts.append(f"허용 행동: {policy['allowed_action']}")

            content = "\n".join(parts)
            total_chars += len(content)

            response = client.embeddings.create(model=EMBED_MODEL, input=content)
            vector = response.data[0].embedding

            conn.execute(text(
                "INSERT INTO policy_chunk "
                "(policy_id, chunk_index, content, embedding, embed_model) "
                "VALUES (:policy_id, 0, :content, :embedding, :model)"
            ), {"policy_id": policy["id"], "content": content,
                "embedding": str(vector), "model": EMBED_MODEL})

    print(f"embedded {len(policies)} policies, {total_chars} chars, model={EMBED_MODEL}")


if __name__ == "__main__":
    main()