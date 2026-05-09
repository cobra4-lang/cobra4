"""cobra4 runtime — helpers and smart dispatchers invoked by transpiled code."""

from cobra4.runtime.smart import SmartFn, smart, AmbiguousDispatch, NoHandler
from cobra4.runtime.core import (
    safe_attr,
    default,
    every,
    on_event,
    schedule_registry,
    event_registry,
    serve_registry,
    deploy_registry,
    reset_registries,
)
from cobra4.runtime.io import read, save
from cobra4.runtime.concurrency import parallel_for
from cobra4.runtime.observe import log
from cobra4.runtime.fleet import Host, inventory, run, fan_out, CommandResult
from cobra4.runtime.secrets import secret, register_backend, use_backend, SecretNotFound
from cobra4.runtime.deploy import (
    deploy,
    DeployTarget,
    register_adapter,
    list_adapters,
    env_from,
    aws,
    gcp,
    azure,
    k8s,
    fly,
)
from cobra4.runtime.schedule import queue, serve_forever, InMemoryQueue
from cobra4.runtime.result import Result, Ok, Err, _c4_try_propagate, _C4Propagate
from cobra4.runtime.workflow import Workflow, WorkflowError
from cobra4.runtime.llm import (
    AgentError, MockProvider, AnthropicProvider, set_provider, _c4_llm_run,
)
from cobra4.runtime.effects import (
    EffectViolation, with_effects as _c4_effect_sandbox, check as _c4_effect_check,
)

__all__ = [
    "SmartFn",
    "smart",
    "AmbiguousDispatch",
    "NoHandler",
    "safe_attr",
    "default",
    "every",
    "on_event",
    "schedule_registry",
    "event_registry",
    "serve_registry",
    "deploy_registry",
    "reset_registries",
    "read",
    "save",
    "parallel_for",
    "log",
    "Host",
    "inventory",
    "run",
    "fan_out",
    "CommandResult",
    "secret",
    "register_backend",
    "use_backend",
    "SecretNotFound",
    "deploy",
    "DeployTarget",
    "register_adapter",
    "list_adapters",
    "env_from",
    "aws",
    "gcp",
    "azure",
    "k8s",
    "fly",
    "queue",
    "serve_forever",
    "InMemoryQueue",
    "Result",
    "Ok",
    "Err",
    "_c4_try_propagate",
    "_C4Propagate",
    "Workflow",
    "WorkflowError",
    "AgentError",
    "MockProvider",
    "AnthropicProvider",
    "set_provider",
    "_c4_llm_run",
    "EffectViolation",
    "_c4_effect_sandbox",
    "_c4_effect_check",
]
