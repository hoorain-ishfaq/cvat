// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import { useEffect, useRef, useState } from 'react';

export enum SocketStatus {
    CONNECTING = 'connecting',
    LIVE = 'live',
    RECONNECTING = 'reconnecting',
    OFFLINE = 'offline',
}

const WS_PATH = '/api/test/ws/class-counts';
const BASE_RETRY_MS = 1000;
const MAX_RETRY_MS = 30000;

// Close codes the server sends when it will never accept this client, so
// retrying would just loop. 1008 covers a policy rejection from the stack.
const FATAL_CLOSE_CODES = [4400, 4401, 4403, 4404, 1008];

function socketUrl(taskId: number): string {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}${WS_PATH}?task_id=${taskId}`;
}

interface Options {
    // Called when the server signals a change, and again after every
    // successful reconnect so anything missed while offline is picked up.
    onChange: () => void;
}

/**
 * Subscribes to class-count changes for a single task.
 *
 * The socket only ever carries a "something changed" signal; the caller
 * re-reads the REST endpoint, so the page has one source of truth and works
 * unchanged if the socket never connects.
 */
export default function useClassCountsSocket(taskId: number, options: Options): SocketStatus {
    const { onChange } = options;
    const [status, setStatus] = useState<SocketStatus>(SocketStatus.CONNECTING);

    // Kept in refs so reconnect scheduling never re-runs the effect and the
    // latest callback is always used without resubscribing.
    const onChangeRef = useRef(onChange);
    const socketRef = useRef<WebSocket | null>(null);
    const timerRef = useRef<number | null>(null);
    const attemptRef = useRef(0);
    const closedByUsRef = useRef(false);
    const hasConnectedRef = useRef(false);

    useEffect(() => {
        onChangeRef.current = onChange;
    }, [onChange]);

    useEffect(() => {
        if (!Number.isInteger(taskId) || taskId < 1) {
            setStatus(SocketStatus.OFFLINE);
            return undefined;
        }

        closedByUsRef.current = false;
        attemptRef.current = 0;
        hasConnectedRef.current = false;

        const clearTimer = (): void => {
            if (timerRef.current !== null) {
                window.clearTimeout(timerRef.current);
                timerRef.current = null;
            }
        };

        // Declared before scheduleRetry so the two can reference each other;
        // the body is assigned below and is always set before any timer fires.
        let connect = (): void => {};

        const scheduleRetry = (): void => {
            if (closedByUsRef.current) {
                return;
            }

            setStatus(SocketStatus.RECONNECTING);
            // Exponential backoff with a ceiling, plus jitter so many tabs do
            // not all reconnect on the same tick after a server restart.
            const delay = Math.min(BASE_RETRY_MS * 2 ** attemptRef.current, MAX_RETRY_MS);
            const jitter = Math.random() * 250;
            attemptRef.current += 1;

            clearTimer();
            timerRef.current = window.setTimeout(() => connect(), delay + jitter);
        };

        connect = (): void => {
            if (closedByUsRef.current) {
                return;
            }

            let socket: WebSocket;
            try {
                socket = new WebSocket(socketUrl(taskId));
            } catch {
                scheduleRetry();
                return;
            }

            socketRef.current = socket;

            socket.onopen = () => {
                attemptRef.current = 0;
                setStatus(SocketStatus.LIVE);

                // Resync after an outage: updates published while the socket
                // was down were never delivered, so re-read once on return.
                if (hasConnectedRef.current) {
                    onChangeRef.current();
                }
                hasConnectedRef.current = true;
            };

            socket.onmessage = (event: MessageEvent) => {
                try {
                    const payload = JSON.parse(event.data);
                    if (payload?.type === 'class_counts_changed') {
                        onChangeRef.current();
                    }
                } catch {
                    // A malformed frame is not worth tearing the socket down for.
                }
            };

            socket.onerror = () => {
                // onclose always follows, so retrying is handled there.
            };

            socket.onclose = (event: CloseEvent) => {
                socketRef.current = null;

                if (closedByUsRef.current) {
                    return;
                }

                if (FATAL_CLOSE_CODES.includes(event.code)) {
                    setStatus(SocketStatus.OFFLINE);
                    return;
                }

                scheduleRetry();
            };
        };

        connect();

        return () => {
            closedByUsRef.current = true;
            clearTimer();
            if (socketRef.current) {
                socketRef.current.onclose = null;
                socketRef.current.close();
                socketRef.current = null;
            }
        };
    }, [taskId]);

    return status;
}
