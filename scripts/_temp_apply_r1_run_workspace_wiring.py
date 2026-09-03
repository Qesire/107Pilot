from pathlib import Path


PATH = Path("src/pilot107/api/http_app.py")


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match, found {count}: {old[:80]!r}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "from pilot107.api.repair_ticket_routes import RepairTicketRoutes\n"
        "from pilot107.api.runtime_watch_routes import RuntimeWatchRoutes\n",
        "from pilot107.api.repair_ticket_routes import RepairTicketRoutes\n"
        "from pilot107.api.run_workspace_routes import RunWorkspaceRoutes\n"
        "from pilot107.api.runtime_watch_routes import RuntimeWatchRoutes\n",
    )
    text = replace_once(
        text,
        "from pilot107.services.remediation_service import RemediationService\n"
        "from pilot107.services.repair_ticket_service import RepairTicketService\n",
        "from pilot107.services.remediation_service import RemediationService\n"
        "from pilot107.services.repair_ticket_service import RepairTicketService\n"
        "from pilot107.services.run_workspace_service import RunWorkspaceService\n",
    )
    text = replace_once(
        text,
        "        evidence_query: EvidenceQueryService,\n"
        "        run_service: RunService | None = None,\n"
        "        contract_service: ContractService | None = None,\n",
        "        evidence_query: EvidenceQueryService,\n"
        "        run_service: RunService | None = None,\n"
        "        run_workspace_service: RunWorkspaceService | None = None,\n"
        "        contract_service: ContractService | None = None,\n",
    )
    text = replace_once(
        text,
        "        self.run_service = run_service\n"
        "        self.contract_service = contract_service\n",
        "        self.run_service = run_service\n"
        "        self.contract_service = contract_service\n"
        "        self.run_workspace_service = run_workspace_service or RunWorkspaceService(\n"
        "            store=store,\n"
        "            contract_store=(\n"
        "                contract_store\n"
        "                or (contract_service.store if contract_service is not None else None)\n"
        "            ),\n"
        "        )\n"
        "        self.run_workspace_routes = RunWorkspaceRoutes(self.run_workspace_service)\n",
    )
    text = replace_once(
        text,
        "        if repair_ticket_response is not None:\n"
        "            return repair_ticket_response\n"
        "        if self.file_routes is not None:\n",
        "        if repair_ticket_response is not None:\n"
        "            return repair_ticket_response\n"
        "        run_workspace_response = self.run_workspace_routes.handle_get(\n"
        "            parts,\n"
        "            params=params,\n"
        "            identity=identity,\n"
        "        )\n"
        "        if run_workspace_response is not None:\n"
        "            return run_workspace_response\n"
        "        if self.file_routes is not None:\n",
    )

    PATH.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
