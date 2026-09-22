from decimal import Decimal, ROUND_HALF_UP

def pct_of(obtained, maximum):
    if not maximum:
        return Decimal("0.00")
    return (Decimal(obtained) / Decimal(maximum) * 100).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)

def grade_for(percent,rules):
    # Thresholds avoid gaps such as 79.995 between adjacent displayed ranges.
    for rule in sorted(rules,key=lambda r:Decimal(str(r["min_percent"])),reverse=True):
        if percent>=Decimal(str(rule["min_percent"])):
            return rule
    return {"letter":"F","grade_point":"0"}

def scale_rules(scale):
    return [{"letter":r.letter,"min_percent":str(r.min_percent),"max_percent":str(r.max_percent),
             "grade_point":str(r.grade_point)} for r in scale.rules.all()]
