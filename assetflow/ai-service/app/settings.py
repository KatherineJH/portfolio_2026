from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    # database_url feild name: 소문자
    # .env 파일의 DATABASE_URL 대소문자 상이한 것은 구별없이 매칭되므로 괜찮음(pydantic-settings가 처리함)
    openai_api_key: str = ""

settings = Settings()