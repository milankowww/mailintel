# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from datetime import date

from ..repositories import Repository


class DashboardService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def triage_overview(
        self,
        limit: int = 50,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> dict[str, object]:
        interest_profiles: list[dict[str, object]] = []
        for row in self.repository.list_interest_profiles():
            profile = dict(row)
            profile["matched_messages"] = [
                dict(message)
                for message in self.repository.list_messages_for_interest_profile(
                    int(row["id"]),
                    limit=limit,
                    from_date=from_date,
                    to_date=to_date,
                )
            ]
            interest_profiles.append(profile)

        return {
            "triage": [
                dict(row)
                for row in self.repository.list_recent_triage(limit=limit, from_date=from_date, to_date=to_date)
            ],
            "interest_profiles": interest_profiles,
        }
