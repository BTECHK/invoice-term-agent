"""Protocol smoke check using the pinned SDK, with no live ERP or model access."""
import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mock_erp.server import server


async def verify(transport, label):
    async with Client(transport) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert len(names) == 9, names
        result = await client.call_tool("get_data_catalog", {})
        assert not result.is_error and isinstance(result.structured_content, dict)
        result = await client.call_tool("run_query", {
            "query": "po_terms", "variables": {"po_reference": "0000000001"}})
        assert not result.is_error and result.structured_content["query"] == "po_terms"
        assert isinstance(result.structured_content["rows"], list)
        for name in ("trigger_erp_action", "start_workflow", "resume_workflow"):
            result = await client.call_tool(name, {})
            assert result.is_error, name
        print(f"{label}: discovery, structured reads, three denied writes, teardown PASS")


async def main():
    await verify(server, "in-process")
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "mock_erp/server.py")], cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT / "src"), "PYTHONNOUSERSITE": "1"})
    await verify(params, "stdio")


if __name__ == "__main__":
    asyncio.run(main())
