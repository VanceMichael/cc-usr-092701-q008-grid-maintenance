"""只追加的命令台账与幂等回执。

现场网络抖动、窗口调整通知重发、客户端重试都可能让同一条命令到达多次。
每条带 ``command_id`` 的命令首次成功后，其结果按幂等键留存；
之后相同键的重试直接返回原回执，内容不一致则判定为伪造重放并拒绝，
从而保证窗口调整、停送电等关键动作不会被重复执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class Receipt:
    """一条命令的处理结果，随台账永久保留。"""

    command_id: str
    idempotency_key: str
    action: str
    actor_id: str
    at: datetime
    ok: bool
    result: dict[str, Any]
    fingerprint: str
    error: Optional[str] = None
    replay: bool = False


@dataclass
class Ledger:
    """命令流水：只追加，不修改、不删除。"""

    _receipts: list[Receipt] = field(default_factory=list)
    _by_key: dict[str, Receipt] = field(default_factory=dict)
    _by_command: dict[str, Receipt] = field(default_factory=dict)

    def lookup(self, idempotency_key: str) -> Optional[Receipt]:
        return self._by_key.get(idempotency_key)

    def lookup_command(self, command_id: str) -> Optional[Receipt]:
        return self._by_command.get(command_id)

    def record(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        action: str,
        actor_id: str,
        at: datetime,
        ok: bool,
        result: dict[str, Any],
        fingerprint: str = "",
        error: Optional[str] = None,
        replay: bool = False,
    ) -> Receipt:
        receipt = Receipt(
            command_id=command_id,
            idempotency_key=idempotency_key,
            action=action,
            actor_id=actor_id,
            at=at,
            ok=ok,
            result=result,
            fingerprint=fingerprint,
            error=error,
            replay=replay,
        )
        self._receipts.append(receipt)
        # 失败不占用幂等键，允许修正后重试；成功结果才作为重放凭证
        if ok and not replay:
            self._by_key[idempotency_key] = receipt
            self._by_command[command_id] = receipt
        return receipt

    def all_receipts(self) -> tuple[Receipt, ...]:
        return tuple(self._receipts)
