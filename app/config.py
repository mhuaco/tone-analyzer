from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    zendesk_subdomain: str
    # OAuth client_credentials creds (Admin Center -> Apps and integrations -> APIs ->
    # OAuth clients). API tokens are being retired by Zendesk starting July 28, 2026.
    zendesk_client_id: str
    zendesk_client_secret: str
    zendesk_webhook_secret: str | None = None
    # Optional: Zendesk organization custom field key holding renewal date, if you track it there.
    # Otherwise pull renewal dates from your CRM and join in the monthly job instead.
    zendesk_renewal_field_id: int | None = None

    # Custom ticket field IDs the app writes AI analysis into. Create these once with
    # `python -m scripts.setup_zendesk_fields` and paste the printed IDs here.
    zendesk_frustration_field_id: int | None = None
    zendesk_confidence_field_id: int | None = None
    zendesk_summary_field_id: int | None = None
    zendesk_analyzed_at_field_id: int | None = None

    anthropic_api_key: str
    llm_model: str = "claude-sonnet-5"

    # Slack incoming webhook URLs. Any of these can be left unset to skip that channel.
    slack_webhook_cs: str | None = None
    slack_webhook_product: str | None = None
    slack_webhook_engineering: str | None = None

    class Config:
        env_file = ".env"

    @field_validator(
        "zendesk_renewal_field_id",
        "zendesk_frustration_field_id",
        "zendesk_confidence_field_id",
        "zendesk_summary_field_id",
        "zendesk_analyzed_at_field_id",
        mode="before",
    )
    @classmethod
    def _blank_env_to_none(cls, value):
        # An unset .env value like `ZENDESK_FRUSTRATION_FIELD_ID=` loads as "" (present,
        # empty), not absent -- normalize that to None so the `int | None` default applies.
        return None if value == "" else value


settings = Settings()
