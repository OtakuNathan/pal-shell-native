from pal.skill.contracts import SkillDescriptor, SkillApplicabilitySTAR
from .remote_setup_manual import PAL_REMOTE_SETUP_MANUAL, PAL_REMOTE_SETUP_SKILL_ID


class NativePluginProvider:
    module_id = "remote"

    def declared_skills(self):
        module_id = self.module_id
        return (
        SkillDescriptor(
            skill_id=PAL_REMOTE_SETUP_SKILL_ID,
            module_id=module_id,
            title="Pal Remote Host Setup",
            summary="Help users install, upgrade and enroll a remote worker, configure execution targets and verify supported remote shell capabilities.",
            manual_text=PAL_REMOTE_SETUP_MANUAL,
            activation_terms=("pal remote setup", "remote worker", "pal-shell-worker", "pal-shell-native",
                              "worker upgrade", "升级worker", "云主机升级", "远端升级", "remote host setup", "remote shell setup",
                              "远端接入", "远端安装", "远程主机配置", "安装remote端", "添加remote", "配置远端", "云主机接入"),
            capability_refs=("skill_search", "skill_inject", "search_tools", "run_shell", "call_tool"),
            applicability_star=SkillApplicabilitySTAR(
                situation="The user wants help adding a remote execution host to Pal.",
                task="Prepare or install a matching remote worker and enroll the execution target.",
                action="Inspect platforms and identities, configure supported capabilities, and verify worker and Pal activation separately.",
                result="A verified target or concrete remaining user step, without exposed credentials or unsupported capability claims.",
            ),
            use_when="Use for remote host onboarding, existing worker upgrades, worker installation, SSH/RPC identity enrollment and execution target setup.",
            avoid_when="Avoid for routine commands on an already configured target or unrelated SSH administration.",
            source_format="internal_skill",
            source_refs=("pal_shell_native.remote_setup_manual", "docs/remote-shell.md", "pal_shell_remote"),
            metadata={"internal": True},
        ),
        )
