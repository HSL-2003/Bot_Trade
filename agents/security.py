import json
from .base_agent import BaseLocalAgent

class AppSecAgent(BaseLocalAgent):
    """AppSec Specialist Agent auditing code against OWASP Top 10 vulnerabilities."""
    def __init__(self, model: str = None):
        super().__init__(
            name="AppSec Agent",
            role_prompt=(
                "You are an Application Security Specialist (AppSec). Audit the code for OWASP Top 10 flaws, "
                "hardcoded secrets, SQL injection, XSS, Broken Auth, CORS misconfigurations, and BOLA. "
                "Your output MUST be a structured JSON object with keys: "
                "'is_secure' (boolean: true/false), "
                "'issues' (list of strings describing vulnerabilities found), "
                "'recommendations' (string with refactoring advice)."
            ),
            model=model
        )

    async def audit(self, code: str) -> dict:
        task = f"Audit the following code for security vulnerabilities:\n\n{code}\n\nReturn JSON output."
        raw_res = await self.execute(task)
        
        # Parse JSON output gracefully
        try:
            json_str = raw_res
            if "```json" in raw_res:
                json_str = raw_res.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_res:
                json_str = raw_res.split("```")[1].split("```")[0].strip()
            
            parsed = json.loads(json_str)
            return {
                "is_secure": bool(parsed.get("is_secure", False)),
                "issues": parsed.get("issues", []),
                "recommendations": parsed.get("recommendations", ""),
                "raw_output": raw_res
            }
        except Exception:
            is_secure = "NO_VULNERABILITY_FOUND" in raw_res.upper() or "IS_SECURE: TRUE" in raw_res.upper() or "PASSED" in raw_res.upper()
            return {
                "is_secure": is_secure,
                "issues": [raw_res] if not is_secure else [],
                "recommendations": "Ensure OWASP Top 10 rules are followed.",
                "raw_output": raw_res
            }
