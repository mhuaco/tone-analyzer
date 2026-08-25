from typing import Literal

from pydantic import BaseModel, Field

ToneCategory = Literal["positive", "neutral", "frustrated", "angry"]
ChurnRisk = Literal["LOW", "MEDIUM", "HIGH"]


class ToneAnalysisResult(BaseModel):
    """Structured output from per-ticket LLM scoring."""

    frustration_score: float = Field(..., ge=0.0, le=10.0, description="0 = delighted, 10 = extremely frustrated/angry")
    tone_category: ToneCategory
    component_tags: list[str] = Field(
        default_factory=list,
        description="Product areas/features/workflows referenced, e.g. 'saml', 'authentication', 'billing'",
    )
    key_signals: list[str] = Field(
        default_factory=list,
        description="Short paraphrased phrases (not verbatim quotes) that justify the score",
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Model's confidence in its own analysis")
    summary: str = Field(..., description="One sentence summarizing the customer's emotional state in this ticket")


class OrgInsight(BaseModel):
    """LLM-generated portion of a customer-health row (merged with computed stats in monthly_job)."""

    org_id: int
    churn_risk: ChurnRisk
    product_confidence: float = Field(..., ge=0.0, le=10.0)
    why: list[str] = Field(default_factory=list, description="Short bullet reasons, e.g. '6 SAML-related tickets'")


class ComponentInsight(BaseModel):
    """LLM-generated portion of a product-health row (merged with computed stats in monthly_job)."""

    component: str
    common_themes: list[str] = Field(default_factory=list, description="Short bullets, e.g. 'Group mapping', 'Configuration'")


class MonthlySynthesisResult(BaseModel):
    """What the LLM returns when given pre-aggregated company/org/component stats."""

    overall_summary: str
    org_insights: list[OrgInsight] = Field(default_factory=list)
    component_insights: list[ComponentInsight] = Field(default_factory=list)
