import { reactive, watch } from 'vue';

// WebChat 模拟身份：仅用于 WebUI 测试，可模拟群聊 / 自定义发送者 id / 群组 id。
// 状态持久化到浏览器 localStorage，随每条聊天请求以 `simulate` 字段下发到后端。

export interface SimulatedIdentity {
    enabled: boolean;
    is_group: boolean;
    group_id: string;
    user_id: string;
    sender_name: string;
    at_bot: boolean;
}

export interface SimulatePayload {
    is_group: boolean;
    group_id: string;
    user_id: string;
    sender_name: string;
    at_bot: boolean;
}

const STORAGE_KEY = 'chat.simulatedIdentity';

function defaultIdentity(): SimulatedIdentity {
    return {
        enabled: false,
        is_group: false,
        group_id: '',
        user_id: '',
        sender_name: '',
        at_bot: true,
    };
}

function readStored(): SimulatedIdentity {
    const base = defaultIdentity();
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return base;
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
            return {
                enabled: Boolean(parsed.enabled),
                is_group: Boolean(parsed.is_group),
                group_id: typeof parsed.group_id === 'string' ? parsed.group_id : '',
                user_id: typeof parsed.user_id === 'string' ? parsed.user_id : '',
                sender_name:
                    typeof parsed.sender_name === 'string' ? parsed.sender_name : '',
                at_bot: parsed.at_bot === undefined ? true : Boolean(parsed.at_bot),
            };
        }
    } catch {
        // ignore malformed storage
    }
    return base;
}

/**
 * 读取当前模拟身份并构造随请求下发的 payload。
 * 未启用时返回 undefined，后端将保持默认私聊行为。
 * 直接读 localStorage，保证发送时取到最新值（与 UI 解耦）。
 */
export function buildSimulatePayload(): SimulatePayload | undefined {
    const identity = readStored();
    if (!identity.enabled) return undefined;
    return {
        is_group: identity.is_group,
        group_id: identity.group_id.trim(),
        user_id: identity.user_id.trim(),
        sender_name: identity.sender_name.trim(),
        at_bot: identity.at_bot,
    };
}

export function useSimulatedIdentity() {
    const identity = reactive<SimulatedIdentity>(readStored());

    watch(
        identity,
        (value) => {
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
            } catch {
                // ignore storage write failures
            }
        },
        { deep: true },
    );

    function reset() {
        Object.assign(identity, defaultIdentity());
    }

    return { identity, reset };
}
