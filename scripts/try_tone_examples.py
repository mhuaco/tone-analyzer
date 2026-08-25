"""Run the tone-analysis skill (app/llm_analyzer.analyze_ticket_tone) against a list of
example transcripts, with no Zendesk or webhook involved -- just the LLM call.

Edit EXAMPLES below with your own transcripts (swap them out, add more, whatever) and re-run:
    python -m scripts.try_tone_examples
"""

from app.llm_analyzer import analyze_ticket_tone

# Each example is (label, transcript). Format doesn't matter beyond "[Customer]: ..." /
# "[Agent]: ..." lines -- that's just convention, not a requirement of the function.
EXAMPLES = [
    (
        "calm bug report",
        """Subject: Export is failing silently

[Customer]: When I click export to CSV nothing happens, no error either. Not urgent, just wanted to flag it.

[Agent]: Thanks for letting us know, looking into it now.""",
    ),
    (
        "escalating frustration",
        """Subject: Still no response after 3 days

[Customer]: This is the third time I'm writing in. My team can't log in and it's been three business days with no update.

[Agent]: Apologies for the delay, we're still investigating.

[Customer]: This is unacceptable. We're paying for enterprise support and getting radio silence.""",
    ),
    (
        "happy / grateful",
        """Subject: Thank you!

[Customer]: Just wanted to say the new dashboard export feature is exactly what we needed. Saved us hours this week.

[Agent]: So glad to hear it! I'll pass this along to the team.""",
    ),
    (
        "angry escalation",
        """Subject: Cancelling immediately

[Customer]: I've asked FOUR times for this billing error to be fixed and nobody has done anything. This is the worst support experience I've ever had. I want a refund and I'm cancelling my subscription today.

[Agent]: I'm very sorry, let me escalate this right away.""",
    ),
    (
        "confused, not frustrated",
        """Subject: How do I add a teammate?

[Customer]: Trying to invite a colleague to my workspace, can't find the option. Is it under settings somewhere?

[Agent]: You'll find it under Settings > Team > Invite.""",
    ),
]


def main() -> None:
    for label, transcript in EXAMPLES:
        result = analyze_ticket_tone(transcript)
        print(f"\n=== {label} ===")
        print(f"tone: {result.tone_category}   frustration: {result.frustration_score}/10   confidence: {result.confidence}")
        print(f"topics: {result.component_tags}")
        print(f"summary: {result.summary}")
        if result.key_signals:
            print("signals:")
            for s in result.key_signals:
                print(f"  - {s}")


if __name__ == "__main__":
    main()
