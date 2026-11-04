"""Pure, versioned scoring policies for Attack & Defense."""

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class TeamTickScore:
    attack_milli: int
    defense_milli: int
    sla_milli: int
    input_digest: str


def weighted_components(config, attack, defense, sla):
    """Return weighted milli-points, rounded once per cumulative component."""
    return tuple(
        int(
            (Decimal(value) * Decimal(str(coefficient))).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        for value, coefficient in zip((attack, defense, sla), (config.ma, config.mb, config.mc))
    )


class WeightedAttackDefenseScoringPolicy:
    """Default full A&D policy; it performs no persistence or runtime I/O."""

    name = "weighted_v2"
    version = 2

    def compute_team(
        self,
        *,
        config,
        team_enrollment_id,
        submissions,
        checks,
        definitions,
        instance_definition_ids,
        expected_instance_ids,
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
        # A tick earns SLA only when every scheduled own flag was retrieved correctly.
        # Missing checks, checker exceptions and timeouts cannot earn availability points.
        retrieved = {c.service_instance_id for c in own_checks if c.flag_ok is True}
        sla = (
            config.sla_points_milli
            if expected_instance_ids
            and (retrieved == expected_instance_ids and all(c.flag_ok is True for c in own_checks))
            else 0
        )
        canonical_input = {
            "policy": [self.name, self.version],
            "coefficients": [config.ma, config.mb, config.mc],
            "submissions": sorted(str(item.id) for item in own_submissions),
            "checks": sorted((str(item.id), item.status, item.flag_ok) for item in own_checks),
            "expected_instances": sorted(str(i) for i in expected_instance_ids),
        }
        digest = hashlib.sha256(
            json.dumps(canonical_input, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return TeamTickScore(attack, defense, sla, digest)
