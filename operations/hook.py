from .policy import check_tool

def evaluate_hook(event, context):
    name = event.get('tool_name','')
    if name == 'mcp__operations__call':
        return {'hookSpecificOutput': {'hookEventName':'PreToolUse','permissionDecision':'allow'}}
    decision = check_tool(name, event.get('tool_input') or {}, context)
    return {'hookSpecificOutput': {'hookEventName':'PreToolUse', 'permissionDecision': 'allow' if decision.get('allowed') else 'deny', 'permissionDecisionReason': decision.get('reason','')}}
