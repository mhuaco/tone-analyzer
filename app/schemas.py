from typing import Literal

from pydantic import BaseModel, Field

ToneCategory = Literal["positive", "neutral", "frustrated", "angry"]
ChurnRisk = Literal["LOW", "MEDIUM", "HIGH"]

# Fixed topic vocabulary -- deliberately closed, not freeform. An earlier freeform version
# produced near-duplicate tags across tickets (e.g. "export" vs "csv_export"), which
# fragmented Explore reporting. Extend this list (and TONE_TOOL's matching enum in
# llm_analyzer.py) when a new product area needs tracking; don't let the model invent one.
Topic = Literal["gateway", "dashboard", "pump", "sync", "mdcb", "operator", "helm_charts", "sso"]


class ToneAnalysisResult(BaseModel):
    """Structured output from per-ticket LLM scoring."""

    frustration_score: float = Field(..., ge=0.0, le=10.0, description="0 = delighted, 10 = extremely frustrated/angry")
    tone_category: ToneCategory
    component_tags: list[Topic] = Field(
        default_factory=list,
        description="Product areas referenced, from the fixed Topic vocabulary. Empty if none clearly apply.",
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
