"""Preserve strict rejection while making the extraction contract explicit."""
from server import notes_facts as nf


def item(field, text, category, value):
    return {"field":field,"start":0,"end":len(text),"span":text,"category":category,
            "value":value,"polarity":"affirmed","subject":"self","strength":"stated"}


def test_job_type_does_not_establish_working_hours():
    text="Barista and hospitality casual"
    data={"items":[item("work",text,"variable_or_night_hours",{"pattern":"variable"})],"residual":True}
    result=nf.validate_reply(data,{"work":text},1)
    assert result["rejected"] and not result["packet_items"]


def test_early_sleep_does_not_establish_early_waking():
    text="I'm an early sleeper on weekdays."
    data={"items":[item("else_text",text,"lifestyle_self_description",{"field":"routine","answer":"Early riser"})],"residual":True}
    result=nf.validate_reply(data,{"else_text":text},1)
    assert result["rejected"] and not result["packet_items"]


def test_reasonably_clean_does_not_silently_relax_existing_grounding():
    text="I keep things reasonably clean and do my share of the chores."
    data={"items":[item("intro",text,"lifestyle_self_description",{"field":"tidy","answer":"Average"})],"residual":True}
    result=nf.validate_reply(data,{"intro":text},1)
    assert result["rejected"] and not result["packet_items"]


def test_unrepresented_practical_text_remains_residual():
    result=nf.validate_reply({"items":[],"residual":True},{"work":"Barista and hospitality casual"},1)
    assert not result["rejected"] and not result["packet_items"]
    assert result["residual"] is True


def test_prompt_and_correction_preserve_no_inference_guidance():
    first=nf.build_prompt({"work":"Barista and hospitality casual"},1)
    corrected=nf.correction_prompt({"work":"Barista and hospitality casual"},1,None,{"items":[]})
    assert "bedtime does not establish waking time" in first
    assert "employment type" in first
    assert "empty items list with residual=true is valid" in first
    assert "Do not replace a rejected item with another inferred category" in corrected
    assert nf.MAX_LOGICAL_REQUESTS==2
