from typing import Any
import httpx
from core.config import get_turso_token, get_turso_url


class TursoError(RuntimeError):
    pass


def _encode_arg(val: Any) -> dict[str, Any]:
    if val is None:
        return {"type": "null", "value": None}
    if isinstance(val, bool):
        return {"type": "integer", "value": "1" if val else "0"}
    if isinstance(val, int):
        return {"type": "integer", "value": str(val)}
    if isinstance(val, float):
        return {"type": "float", "value": val}
    return {"type": "text", "value": str(val)}


def _decode_cell(cell: dict[str, Any]) -> Any:
    ctype = cell.get("type")
    val = cell.get("value")
    if ctype == "null" or val is None:
        return None
    if ctype == "integer":
        return int(val)
    if ctype == "float":
        return float(val)
    return str(val)


class TursoClient:
    """Lightweight async HTTP client for Turso (/v2/pipeline) with zero native C dependencies."""

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.base_url = (url or get_turso_url()).rstrip("/")
        self.token = token or get_turso_token()

    @property
    def pipeline_url(self) -> str:
        if not self.base_url:
            raise TursoError("TURSO_DATABASE_URL не задан в переменных окружения.")
        return f"{self.base_url}/v2/pipeline"

    async def execute_batch(
        self,
        statements: list[tuple[str, list[Any] | tuple[Any, ...]]],
        client: httpx.AsyncClient | None = None,
    ) -> list[list[dict[str, Any]]]:
        requests_payload: list[dict[str, Any]] = []
        for sql, args in statements:
            stmt_obj: dict[str, Any] = {"sql": sql}
            if args:
                stmt_obj["args"] = [_encode_arg(a) for a in args]
            requests_payload.append({"type": "execute", "stmt": stmt_obj})
        requests_payload.append({"type": "close"})

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await http.post(
                self.pipeline_url,
                headers=headers,
                json={"requests": requests_payload},
            )
            if resp.status_code >= 400:
                raise TursoError(f"Turso HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
        finally:
            if owns_client:
                await http.aclose()

        parsed_results: list[list[dict[str, Any]]] = []
        for item in data.get("results", []):
            if item.get("type") == "error":
                err = item.get("error", {})
                raise TursoError(f"Turso SQL Error: {err.get('message', err)}")
            inner = item.get("response", {})
            if inner.get("type") != "execute":
                continue
            res_body = inner.get("result", {})
            cols = [c.get("name", f"col_{i}") for i, c in enumerate(res_body.get("cols", []))]
            rows_out: list[dict[str, Any]] = []
            for raw_row in res_body.get("rows", []):
                row_dict = {cols[i]: _decode_cell(cell) for i, cell in enumerate(raw_row)}
                rows_out.append(row_dict)
            parsed_results.append(rows_out)
        return parsed_results

    async def execute(
        self,
        sql: str,
        args: list[Any] | tuple[Any, ...] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]:
        res = await self.execute_batch([(sql, list(args or []))], client=client)
        return res[0] if res else []
