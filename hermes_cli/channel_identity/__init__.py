"""Trusted channel identity and durable queue storage."""

from .crypto import ChannelCrypto, Keyring
from .employee_profiles import (
    EmployeeProfileRevisionConflict,
    FeishuCredentialRevisionConflict,
    claim_existing_feishu_account_for_owner,
    employee_profile_fingerprint,
    ensure_managed_feishu_conversation_binding,
    list_managed_feishu_accounts,
    register_managed_feishu_account_for_owner,
    resolve_employee_profile,
    resolve_managed_feishu_account,
    resolve_managed_feishu_credentials,
    rollover_managed_feishu_sessions,
    rotate_managed_feishu_credentials,
    set_managed_feishu_account_status,
    update_employee_profile,
)
from .models import (
    EmployeeProfile,
    ManagedFeishuAccount,
    RegisteredChannel,
    ResolvedChannelOwner,
    ResolvedConnectorAccount,
)
from .owner_resolution import resolve_binding, resolve_connector_account
from .registration import (
    ChannelIdentityOwnershipConflict,
    activate_weixin_identity,
    ensure_owner_binding,
    register_connector_binding_for_owner,
    register_weixin_identity,
    register_weixin_identity_for_owner,
)
from .store import ChannelIdentityStore

__all__ = [
    "ChannelCrypto",
    "ChannelIdentityOwnershipConflict",
    "ChannelIdentityStore",
    "EmployeeProfile",
    "EmployeeProfileRevisionConflict",
    "FeishuCredentialRevisionConflict",
    "Keyring",
    "ManagedFeishuAccount",
    "RegisteredChannel",
    "ResolvedChannelOwner",
    "ResolvedConnectorAccount",
    "activate_weixin_identity",
    "claim_existing_feishu_account_for_owner",
    "employee_profile_fingerprint",
    "ensure_managed_feishu_conversation_binding",
    "ensure_owner_binding",
    "list_managed_feishu_accounts",
    "register_connector_binding_for_owner",
    "register_managed_feishu_account_for_owner",
    "register_weixin_identity",
    "register_weixin_identity_for_owner",
    "resolve_binding",
    "resolve_connector_account",
    "resolve_employee_profile",
    "resolve_managed_feishu_account",
    "resolve_managed_feishu_credentials",
    "rollover_managed_feishu_sessions",
    "rotate_managed_feishu_credentials",
    "set_managed_feishu_account_status",
    "update_employee_profile",
]
