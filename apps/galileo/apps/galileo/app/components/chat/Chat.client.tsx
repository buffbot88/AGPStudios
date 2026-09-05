import { useStore } from '@nanostores/react';
import type { Message } from 'ai';
import { useAnimate } from 'framer-motion';
import { memo, useEffect, useRef, useState } from 'react';
import { cssTransition, toast, ToastContainer } from 'react-toastify';
import { useMessageParser, usePromptEnhancer, useShortcuts, useSnapScroll } from '~/lib/hooks';
import { enhanceText } from '~/lib/hooks/usePromptEnhancer';
import { runAgentTurn } from '~/lib/runtime/agent-controller';
import type { AgentEvent } from '~/lib/runtime/galileo-stream';
import { useChatHistory } from '~/lib/persistence';
import { chatStore } from '~/lib/stores/chat';
import { workbenchStore } from '~/lib/stores/workbench';
import { autoEnhance } from '~/lib/stores/ui';
import { webcontainer } from '~/lib/webcontainer';
import { fileModificationsToHTML } from '~/utils/diff';
import { cubicEasingFn } from '~/utils/easings';
import { friendlyChatErrorMessage } from '~/utils/chat-errors';
import { createScopedLogger, renderLogger } from '~/utils/logger';
import { BaseChat } from './BaseChat';

const toastAnimation = cssTransition({
  enter: 'animated fadeInRight',
  exit: 'animated fadeOutRight',
});

const logger = createScopedLogger('Chat');

export function Chat() {
  renderLogger.trace('Chat');

  const { project, ready, initialMessages, storeMessageHistory } = useChatHistory();

  return (
    <>
      {ready && <ChatImpl project={project} initialMessages={initialMessages} storeMessageHistory={storeMessageHistory} />}
      <ToastContainer
        closeButton={({ closeToast }) => {
          return (
            <button className="Toastify__close-button" onClick={closeToast}>
              <div className="i-ph:x text-lg" />
            </button>
          );
        }}
        icon={({ type }) => {
          /**
           * @todo Handle more types if we need them. This may require extra color palettes.
           */
          switch (type) {
            case 'success': {
              return <div className="i-ph:check-bold text-bolt-elements-icon-success text-2xl" />;
            }
            case 'error': {
              return <div className="i-ph:warning-circle-bold text-bolt-elements-icon-error text-2xl" />;
            }
          }

          return undefined;
        }}
        position="bottom-right"
        pauseOnFocusLoss
        transition={toastAnimation}
      />
    </>
  );
}

interface ChatProps {
  project?: boolean;
  initialMessages: Message[];
  storeMessageHistory: (messages: Message[]) => Promise<void>;
}

export const ChatImpl = memo(({ project, initialMessages, storeMessageHistory }: ChatProps) => {
  useShortcuts();

  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const [chatStarted, setChatStarted] = useState(initialMessages.length > 0);
  const [streamingTool, setStreamingTool] = useState<{ name: string; args: Record<string, unknown> } | null>(null);
  const [activityEvents, setActivityEvents] = useState<AgentEvent[]>([]);
  const [activityStartedAt, setActivityStartedAt] = useState<number | null>(null);
  const [agentRunning, setAgentRunning] = useState(false);
  const [messages, setMessages] = useState<Message[]>(initialMessages);
  const [input, setInput] = useState('');
  const agentAbort = useRef<AbortController | null>(null);

  const { showChat } = useStore(chatStore);

  const [animationScope, animate] = useAnimate();


  const { enhancingPrompt, promptEnhanced, enhancePrompt, resetEnhancer } = usePromptEnhancer();

  useEffect(() => {
    let hydrated = false;
    let timer: number | undefined;
    fetch(`/api/projects?project_id=${encodeURIComponent(window.location.pathname.split('/').pop() || 'default')}`, { credentials: 'include' })
      .then(async (response) => response.ok ? await response.json() as { files: Record<string, string>; project?: string } : { files: {} as Record<string, string> })
      .then(async ({ files }) => {
        const container = await webcontainer;
        for (const entry of await container.fs.readdir('.')) {
          await container.fs.rm(entry, { recursive: true });
        }
        for (const [filePath, content] of Object.entries(files)) {
          const directory = filePath.split('/').slice(0, -1).join('/');
          if (directory) await container.fs.mkdir(directory, { recursive: true });
          await container.fs.writeFile(filePath, content);
        }
        const packageJson = files['package.json'];
        if (packageJson) {
          try {
            const scripts = (JSON.parse(packageJson) as { scripts?: Record<string, string> }).scripts || {};
            const script = scripts.dev ? 'dev' : scripts.start ? 'start' : undefined;
            if (script) {
              const install = await container.spawn('npm', ['install']);
              if (await install.exit) void container.spawn('jsh', ['-c', `npm run ${script} -- --host 0.0.0.0`]);
            }
          } catch {
            // Invalid package manifests are left for the user to fix in the workspace.
          }
        }
        hydrated = true;
      })
      .catch(() => { hydrated = true; });
    const unsubscribe = workbenchStore.files.subscribe((files) => {
      if (!hydrated) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        const snapshot = Object.fromEntries(Object.entries(files).filter(([, file]) => file?.type === 'file' && !file.isBinary).map(([filePath, file]) => [filePath.replace(/^.*?\/home\/project\//, ''), (file as { content: string }).content || '']));
        if (Object.keys(snapshot).length) void fetch('/api/projects', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: window.location.pathname.split('/').pop() || 'default', files: snapshot }) });
      }, 1500);
    });
    return () => { unsubscribe(); window.clearTimeout(timer); };
  }, []);
  const { parsedMessages, parseMessages } = useMessageParser();
  const TEXTAREA_MAX_HEIGHT = chatStarted ? 400 : 200;

  useEffect(() => {
    chatStore.setKey('started', initialMessages.length > 0);
  }, []);

  useEffect(() => {
    parseMessages(messages, agentRunning);

    if (messages.length > 0) {
      storeMessageHistory(messages).catch((error) => toast.error(error.message));
    }
  }, [messages, agentRunning, parseMessages]);

  const scrollTextArea = () => {
    const textarea = textareaRef.current;

    if (textarea) {
      textarea.scrollTop = textarea.scrollHeight;
    }
  };

  const abort = () => {
    agentAbort.current?.abort();
    chatStore.setKey('aborted', true);
    workbenchStore.abortAllActions();
  };

  useEffect(() => {
    const textarea = textareaRef.current;

    if (textarea) {
      textarea.style.height = 'auto';

      const scrollHeight = textarea.scrollHeight;

      textarea.style.height = `${Math.min(scrollHeight, TEXTAREA_MAX_HEIGHT)}px`;
      textarea.style.overflowY = scrollHeight > TEXTAREA_MAX_HEIGHT ? 'auto' : 'hidden';
    }
  }, [input, textareaRef]);

  const runAnimation = async () => {
    if (chatStarted) {
      return;
    }

    await Promise.all([
      animate('#examples', { opacity: 0, display: 'none' }, { duration: 0.1 }),
      animate('#intro', { opacity: 0, flex: 1 }, { duration: 0.2, ease: cubicEasingFn }),
    ]);

    chatStore.setKey('started', true);

    setChatStarted(true);
  };

  const runCustomTurn = async (content: string, context: string) => {
    const controller = new AbortController();
    agentAbort.current = controller;
    setAgentRunning(true);
    setActivityEvents([]);
    setActivityStartedAt(Date.now());
    const now = Date.now();
    const userMessage = { id: crypto.randomUUID(), role: 'user' as const, content, createdAt: new Date(now) };
    let assistant = { id: crypto.randomUUID(), role: 'assistant' as const, content: '', createdAt: new Date(now) };
    const conversation = [...messages, userMessage, assistant];
    setMessages(conversation);
    try {
      for await (const event of runAgentTurn([...messages, userMessage], { projectContext: context, signal: controller.signal })) {
        setActivityEvents((current) => [...current, event]);
        if (event.type === 'tool.start') setStreamingTool({ name: event.name, args: {} });
        if (event.type === 'tool.arguments' && typeof event.arguments === 'object' && event.arguments !== null) setStreamingTool((current) => current ? { ...current, args: event.arguments as Record<string, unknown> } : current);
        if (event.type === 'tool.result') setStreamingTool(null);
        if (event.type === 'text.delta') {
          assistant = { ...assistant, content: assistant.content + event.delta };
          conversation[conversation.length - 1] = assistant;
          setMessages([...conversation]);
        }
        if (event.type === 'error') throw new Error(event.message);
      }
    } catch (error) {
      if (!controller.signal.aborted && !chatStore.get().aborted) {
        logger.error('Request failed\n\n', error);
        toast.error(friendlyChatErrorMessage(error), { autoClose: 8000 });
      }
    } finally {
      agentAbort.current = null;
      setStreamingTool(null);
      setAgentRunning(false);
    }
  };

  const sendMessage = async (_event: React.UIEvent, messageInput?: string) => {
    const _input = messageInput || input;

    if (_input.length === 0 || agentRunning) {
      return;
    }

    let turnInput = _input;

    // Auto-enhance rewrites the prompt through the enhancer before the turn runs.
    if (autoEnhance.get().enabled) {
      const enhanced = await enhanceText(_input);

      if (enhanced !== null) {
        turnInput = enhanced;
      }
    }

    /**
     * @note (delm) Usually saving files shouldn't take long but it may take longer if there
     * many unsaved files. In that case we need to block user input and show an indicator
     * of some kind so the user is aware that something is happening. But I consider the
     * happy case to be no unsaved files and I would expect users to save their changes
     * before they send another message.
     */
    await workbenchStore.saveAllFiles();

    const fileModifications = workbenchStore.getFileModifcations();
    chatStore.setKey('aborted', false);

    runAnimation();

    if (fileModifications !== undefined) {
      const diff = fileModificationsToHTML(fileModifications);

      /**
       * If we have file modifications we append a new user message manually since we have to prefix
       * the user input with the file modifications and we don't want the new user input to appear
       * in the prompt. Using `append` is almost the same as `handleSubmit` except that we have to
       * manually reset the input and we'd have to manually pass in file attachments. However, those
       * aren't relevant here.
       */
      await runCustomTurn(`${diff}\n\n${turnInput}`, '');

      /**
       * After sending a new message we reset all modifications since the model
       * should now be aware of all the changes.
       */
      workbenchStore.resetAllFileModifications();
    } else {
      await runCustomTurn(turnInput, '');
    }

    setInput('');

    resetEnhancer();

    textareaRef.current?.blur();
  };

  const editMessage = (index: number, content: string) => {
    setMessages(messages.slice(0, index));
    setInput(content);
    textareaRef.current?.focus();
  };

  const resendMessage = (index: number) => {
    const message = messages[index];
    if (message?.role !== 'user' || agentRunning) return;
    setMessages(messages.slice(0, index));
    void runCustomTurn(message.content, '');
  };

  const [messageRef, scrollRef] = useSnapScroll();

  return (
    <BaseChat
      ref={animationScope}
      textareaRef={textareaRef}
      input={input}
      showChat={showChat}
      chatStarted={chatStarted}
      isStreaming={agentRunning}
      streamingTool={streamingTool}
      activityEvents={activityEvents}
      activityStartedAt={activityStartedAt}
      enhancingPrompt={enhancingPrompt}
      promptEnhanced={promptEnhanced}
      sendMessage={sendMessage}
      messageRef={messageRef}
      scrollRef={scrollRef}
      handleInputChange={(event) => setInput(event.target.value)}
      handleStop={abort}
      messages={messages.map((message, i) => {
        if (message.role === 'user') {
          return message;
        }

        return {
          ...message,
          content: parsedMessages[i] || '',
        };
      })}
      onEdit={(index, content) => editMessage(index, content)}
      onResend={resendMessage}
      enhancePrompt={() => {
        enhancePrompt(input, (value) => {
          setInput(value);
          scrollTextArea();
        });
      }}
    />
  );
});
