from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig


class MemoryTrimmerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.system_prompt = self.config.get("system_prompt", "你是一个 helpful assistant。")
        self.max_rounds = int(self.config.get("max_history_rounds", 5))
        self.clean_strategy = self.config.get("clean_strategy", "hard")  # hard | progressive | length

        # 渐进参数
        self.prog_trigger = int(self.config.get("progressive_trigger_rounds", 30))
        self.prog_keep = int(self.config.get("progressive_keep_rounds", 15))
        self.prog_ratio = float(self.config.get("progressive_clean_ratio", 0.3))

        # 长度裁剪参数
        self.max_length_kb = int(self.config.get("max_history_length_kb", 800))
        self.keep_length_kb = int(self.config.get("max_keep_length_kb", 400))

        logger.info(
            f"记忆修剪器 v2.3 已加载，策略: {self.clean_strategy}，"
            f"system: {self.system_prompt[:30]}..."
        )

    def _count_rounds(self, contexts: list) -> int:
        return sum(1 for ctx in contexts if ctx.get("role") == "user")

    def _get_message_length(self, content) -> int:
        """计算一条消息的字符长度，兼容 str 和 list（多模态）"""
        if isinstance(content, str):
            return len(content)
        elif isinstance(content, list):
            total = 0
            for item in content:
                if isinstance(item, dict):
                    if "text" in item:
                        total += len(item["text"])
                    elif "image_url" in item:
                        total += 1000  # 图片统一按 1000 字符估算
            return total
        return 0

    def _estimate_length(self, contexts: list) -> int:
        """估算上下文总字符数（中英文混合，1字符≈0.5~1 token）"""
        total = 0
        for ctx in contexts:
            total += self._get_message_length(ctx.get("content", ""))
        return total

    def _sanitize_contexts(self, contexts: list) -> list:
        """清理上下文，防止截断导致 tool 消息孤立或 tool_calls 悬空"""
        if not contexts:
            return contexts
            
        system_msg = []
        if contexts[0].get("role") == "system":
            system_msg = [contexts[0]]
            contexts = contexts[1:]

        # 1. 如果开头是 tool，说明它的 assistant(tool_calls) 被切掉了，直接丢弃
        while contexts and contexts[0].get("role") == "tool":
            contexts.pop(0)

        # 2. 如果结尾是带 tool_calls 的 assistant，说明它的 tool 结果被切掉了，直接丢弃
        if contexts and contexts[-1].get("role") == "assistant" and contexts[-1].get("tool_calls"):
            contexts.pop()

        return system_msg + contexts

    def _get_last_n_rounds(self, contexts: list, n: int) -> list:
        """保留最近 n 轮（user + 对应的 assistant），且保留 system"""
        if not contexts:
            return []
        user_indices = [i for i, ctx in enumerate(contexts) if ctx.get("role") == "user"]
        if len(user_indices) <= n:
            return self._sanitize_contexts(contexts)
        keep_start = user_indices[-n]
        new_contexts = [contexts[0]] if contexts and contexts[0].get("role") == "system" else []
        new_contexts.extend(contexts[keep_start:])
        return self._sanitize_contexts(new_contexts)

    def _progressive_clean(self, contexts: list) -> list:
        """渐进式清理：每次删掉最早的一部分，保留最近"""
        if not contexts:
            return contexts
        rounds = self._count_rounds(contexts)
        if rounds <= self.prog_trigger:
            return contexts
        excess = rounds - self.prog_keep
        if excess <= 0:
            return contexts
        to_remove = max(1, int(excess * self.prog_ratio))
        if to_remove > excess:
            to_remove = excess
        new_keep = rounds - to_remove
        if new_keep < self.prog_keep:
            new_keep = self.prog_keep
        return self._get_last_n_rounds(contexts, new_keep)

    def _length_based_clean(self, contexts: list) -> list:
        """长度裁剪：超过阈值时裁掉前面的内容，直到降到保留量以下"""
        if not contexts:
            return contexts

        current_len = self._estimate_length(contexts)
        current_kb = current_len / 1024

        if current_kb <= self.max_length_kb:
            logger.debug(f"长度裁剪: 当前 {current_kb:.1f}KB <= {self.max_length_kb}KB，不触发")
            return contexts

        logger.info(f"长度裁剪触发: 当前 {current_kb:.1f}KB，目标降至 {self.keep_length_kb}KB 以下")

        system_msg = None
        other_msgs = []
        for ctx in contexts:
            if ctx.get("role") == "system":
                if system_msg is None:
                    system_msg = ctx  # 只取第一个 system
            else:
                other_msgs.append(ctx)

        if not other_msgs:
            return contexts

        # 从后往前遍历，保留最近的，直到长度满足要求
        keep_msgs = []
        total_len = self._get_message_length(system_msg.get("content", "")) if system_msg else 0
        target_bytes = self.keep_length_kb * 1024

        for ctx in reversed(other_msgs):
            ctx_len = self._get_message_length(ctx.get("content", ""))
            if total_len + ctx_len <= target_bytes:
                keep_msgs.insert(0, ctx)
                total_len += ctx_len
            else:
                # 如果加上这条超了，检查是否至少保留一条消息
                if not keep_msgs:
                    # 如果没有保留任何消息，则保留当前这条（即使超一点）
                    keep_msgs.insert(0, ctx)
                    total_len += ctx_len
                    logger.debug(f"长度裁剪: 仅保留最后一条消息 (长度 {ctx_len} 字符)")
                # 否则丢弃这条，继续往前
                break

        # 如果 keep_msgs 为空（理论上不会发生），保底保留最后一条
        if not keep_msgs and other_msgs:
            keep_msgs = [other_msgs[-1]]

        result = []
        if system_msg:
            result.append(system_msg)
        result.extend(keep_msgs)
        
        # 外科手术清理
        result = self._sanitize_contexts(result)

        new_len = self._estimate_length(result)
        new_kb = new_len / 1024
        logger.info(f"长度裁剪完成: {current_kb:.1f}KB → {new_kb:.1f}KB，保留 {self._count_rounds(result)} 轮")

        return result

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not req.contexts:
            return

        # 1. 强制固定 system prompt
        if not req.contexts or req.contexts[0].get("role") != "system":
            req.contexts.insert(0, {"role": "system", "content": self.system_prompt})
        else:
            req.contexts[0]["content"] = self.system_prompt

        # 2. 根据策略裁剪历史
        if self.clean_strategy == "hard":
            req.contexts = self._get_last_n_rounds(req.contexts, self.max_rounds)
        elif self.clean_strategy == "progressive":
            req.contexts = self._progressive_clean(req.contexts)
        elif self.clean_strategy == "length":
            req.contexts = self._length_based_clean(req.contexts)
        else:
            logger.warning(f"未知的清理策略: {self.clean_strategy}，使用 hard 模式")
            req.contexts = self._get_last_n_rounds(req.contexts, self.max_rounds)

        logger.debug(f"记忆修剪器处理后上下文轮数: {self._count_rounds(req.contexts)}")

    async def terminate(self):
        logger.info("记忆修剪器插件已卸载")