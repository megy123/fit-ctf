"""Pure, versioned scoring policies for Attack & Defense."""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class TeamTickScore:
    attack_milli: int
    defense_milli: int
    sla_milli: int
    input_digest: str


class AdditiveAttackDefenseScoringPolicy:
    """Default full A&D policy; it performs no persistence or runtime I/O."""

    name = "additive_v1"
    version = 1

    def compute_team(
        self,
        *,
        config,
        team_enrollment_id,
        submissions,
        checks,
        definitions,
        instance_definition_ids,
    ) -> TeamTickScore:
        own_submissions = [
            item for item in submissions if item.attacker_team_enrollment_id == team_enrollment_id
        ]
        own_checks = [
            item for item in checks if item.owner_team_enrollment_id == team_enrollment_id
        ]
        attack = sum(
            config.attack_points_milli
            * definitions[instance_definition_ids[item.service_instance_id]].scoring_weight_milli
            // 1000
            for item in own_submissions
        )
        defense = sum(
            config.defense_points_milli
            * definitions[item.service_definition_id].scoring_weight_milli
            // 1000
            for item in own_checks
            if item.status == "up"
        )
        scoreable_checks = [item for item in own_checks if item.status != "checker_error"]
        sla = (
            config.sla_points_milli
            * sum(1 for item in scoreable_checks if item.status == "up")
            // len(scoreable_checks)
            if scoreable_checks
            else 0
        )
        canonical_input = {
            "policy": [self.name, self.version],
            "submissions": sorted(str(item.id) for item in own_submissions),
            "checks": sorted((str(item.id), item.status) for item in own_checks),
        }
        digest = hashlib.sha256(
            json.dumps(canonical_input, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return TeamTickScore(attack, defense, sla, digest)
