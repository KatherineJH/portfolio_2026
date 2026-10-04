"""규정 검색. 질문을 임베딩해 가장 가까운 규정을 찾는다."""

from openai import OpenAI
from sqlalchemy import Connection, text

from app.settings import settings

EMBED_MODEL = "text-embedding-3-small"


def _has_policy_subject(conn: Connection) -> bool:
    """후속 policy.subject 컬럼이 있는 DB인지 확인한다."""
    return bool(conn.execute(text(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.columns "
        "  WHERE table_schema = 'public' AND table_name = 'policy' "
        "  AND column_name = 'subject'"
        ")"
    )).scalar_one())


def search_policies(conn: Connection, *, question: str, limit: int = 3) -> list[dict]:
    """질문과 가장 가까운 규정을 거리 순으로 돌려준다."""
    # 구버전 DB에는 policy.subject가 없다. DB를 변경하지 않고 동일한 정책 키를
    # 서버의 주제 값으로 변환한다. 컬럼이 있는 DB에서는 저장된 값을 사용한다.
    subject_sql = (
        "p.subject::text"
        if _has_policy_subject(conn)
        else "CASE p.policy_key "
             "WHEN 'INSPECTION-REQUIRED' THEN 'inspection' "
             "WHEN 'REPLACE-QTY' THEN 'quantity' "
             "WHEN 'REPLACE-STOCK' THEN 'stock' "
             "WHEN 'OWNERSHIP' THEN 'ownership' "
             "WHEN 'COST-LIABILITY' THEN 'liability' "
             "ELSE 'replacement' END"
    )

    chunk_count = conn.execute(text(
        "SELECT count(*) FROM policy_chunk WHERE embed_model = :model"
    ), {"model": EMBED_MODEL}).scalar_one()

    if chunk_count == 0:
        # 기존 DB에는 정책 원문만 있고 임베딩 청크가 없다. DB를 변경하지 않는
        # 호환 모드에서는 원문 전체를 후보로 넘기고, 이후 서버 경로가 인용할
        # subject 하나를 고른다. 이 경로는 벡터 검색이라고 부르지 않는다.
        rows = conn.execute(text(
            f"SELECT p.policy_key, p.version, {subject_sql} AS subject, "
            "       concat_ws(E'\\n', p.condition_text, p.exception_text, "
            "                 p.required_info, p.allowed_action) AS content, "
            "       NULL::double precision AS distance "
            "FROM policy p ORDER BY p.policy_key, p.version"
        )).mappings().all()
        return [dict(row) for row in rows]

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.embeddings.create(model=EMBED_MODEL, input=question)
    vector = str(response.data[0].embedding)

    # <=> 가 pgvector의 코사인 거리 연산자입니다.
    # 값이 작을수록 가깝습니다. ORDER BY에 그대로 써서 가까운 순으로 정렬합니다.

    rows = conn.execute(text(
        f"SELECT p.policy_key, p.version, {subject_sql} AS subject, c.content, "
        "       c.embedding <=> :vector AS distance "
        "FROM policy_chunk c "
        "JOIN policy p ON p.id = c.policy_id "
        "WHERE c.embed_model = :model "
        "ORDER BY c.embedding <=> :vector "
        "LIMIT :limit"
    ), {"vector": vector, "model": EMBED_MODEL, "limit": limit}).mappings().all()
    # WHERE c.embed_model = :model이 중요. 
    # 나중에 임베딩 모델을 바꾸면 차원도 의미도 달라지는데, 옛 모델로 만든 벡터가 섞여 있으면
    # 말이 안 되는 검색 결과가 조용히 나오기때문에 모델이 다른 벡터는 아예 후보에서 빼야한다.
    
    return [dict(row) for row in rows]
