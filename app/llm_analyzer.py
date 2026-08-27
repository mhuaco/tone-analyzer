import anthropic

from app.config import settings
from app.schemas import ToneAnalysisResult

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

TONE_TOOL = {
    "name": "record_tone_analysis",
    "description": "Record structured tone analysis for a customer support ticket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "frustration_score": {
                "type": "number",
                "description": "0 (delighted) to 10 (extremely frustrated/angry)",
            },
            "tone_category": {
                "type": "string",
                "enum": ["positive", "neutral", "frustrated", "angry"],
            },
            "component_tags": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["gateway", "dashboard", "pump", "sync", "mdcb", "operator", "helm_charts", "sso"],
                },
                "description": (
                    "Product areas clearly referenced, from this fixed list only -- do not invent others. "
                    "'sso' covers Identity Broker. Leave empty if none of these are clearly involved."
                ),
            },
            "key_signals": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short paraphrased phrases (not verbatim quotes) supporting the score",
            },
            "confidence": {"type": "number", "description": "0.0 to 1.0"},
            "summary": {"type": "string"},
        },
        "required": ["frustration_score", "tone_category", "confidence", "summary"],
    },
}

SYSTEM_PROMPT = """You are a customer support tone analyst. You read a support ticket \
transcript (customer and agent messages) and assess the CUSTOMER's emotional tone -- \
not the agent's. Consider frustration that persists even if the ticket was marked solved, \
hesitation, confusion, or genuine satisfaction. Be conservative: only assign a high \
frustration_score when the language clearly supports it, not merely because an issue was \
reported. A customer calmly reporting a bug is not automatically 'frustrated' -- reserve \
that category for tickets with clear emotional signal (repeated escalation, expressions of \
concern about reliability, explicit frustration language, etc). Paraphrase supporting \
evidence in key_signals -- do not copy exact customer wording. For component_tags, only pick \
from the fixed list given in the tool schema, and only when a topic is clearly involved -- \
never guess, and never tag more than one or two unless the ticket genuinely spans them."""


def analyze_ticket_tone(transcript: str) -> ToneAnalysisResult:
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[TONE_TOOL],
        tool_choice={"type": "tool", "name": "record_tone_analysis"},
        messages=[
            {
                "role": "user",
                "content": f"Analyze the customer tone in this support ticket:\n\n{transcript}",
            }
        ],
    )

    tool_use_block = next(b for b in response.content if b.type == "tool_use")
    return ToneAnalysisResult(**tool_use_block.input)
