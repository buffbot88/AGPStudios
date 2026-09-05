import type { Message } from 'ai';
import type { AgentEvent } from '~/lib/runtime/galileo-stream';
import React, { type RefCallback } from 'react';
import { useStore } from '@nanostores/react';
import { ClientOnly } from 'remix-utils/client-only';
import { InfoRail } from '~/components/rail/InfoRail.client';
import { Sidebar } from '~/components/sidebar/Sidebar.client';
import { Workbench } from '~/components/workbench/Workbench.client';
import { autoEnhance } from '~/lib/stores/ui';
import { classNames } from '~/utils/classNames';
import { Messages } from './Messages.client';

import styles from './BaseChat.module.scss';

interface BaseChatProps {
  textareaRef?: React.RefObject<HTMLTextAreaElement> | undefined;
  messageRef?: RefCallback<HTMLDivElement> | undefined;
  scrollRef?: RefCallback<HTMLDivElement> | undefined;
  showChat?: boolean;
  chatStarted?: boolean;
  isStreaming?: boolean;
  messages?: Message[];
  onEdit?: (index: number, content: string) => void;
  onResend?: (index: number) => void;
  enhancingPrompt?: boolean;
  promptEnhanced?: boolean;
  input?: string;
  handleStop?: () => void;
  sendMessage?: (event: React.UIEvent, messageInput?: string) => void;
  handleInputChange?: (event: React.ChangeEvent<HTMLTextAreaElement>) => void;
  enhancePrompt?: () => void;
  jobEvents?: string[];
  streamingTool?: { name: string; args: Record<string, unknown> } | null;
  activityEvents?: AgentEvent[];
  activityStartedAt?: number | null;
}

const EXAMPLE_PROMPTS = [
  { text: 'Build a todo app in React using Tailwind' },
  { text: 'Build a simple blog using Astro' },
  { text: 'Create a cookie consent form using Material UI' },
  { text: 'Make a space invaders game' },
  { text: 'How do I center a div?' },
];

const TEXTAREA_MIN_HEIGHT = 64;

export const BaseChat = React.forwardRef<HTMLDivElement, BaseChatProps>(
  (
    {
      textareaRef,
      messageRef,
      scrollRef,
      showChat = true,
      chatStarted = false,
      isStreaming = false,
      enhancingPrompt = false,
      promptEnhanced = false,
      messages = [],
      onEdit,
      onResend,
      input = '',
      sendMessage,
      handleInputChange,
      enhancePrompt,
      handleStop,
      jobEvents = [],
      streamingTool = null,
      activityEvents = [],
      activityStartedAt = null,
    },
    ref,
  ) => {
    const TEXTAREA_MAX_HEIGHT = chatStarted ? 400 : 200;
    const autoEnhanceOn = useStore(autoEnhance);

    return (
      <div
        ref={ref}
        className={classNames(
          styles.BaseChat,
          'relative flex h-full w-full overflow-hidden bg-bolt-elements-background-depth-1',
        )}
        data-chat-visible={showChat}
      >
        <div className="galileo-shell">
          <ClientOnly>{() => <Sidebar />}</ClientOnly>

          <div className="galileo-shell-main">
            <div ref={scrollRef} className="flex overflow-y-auto w-full h-full">
              <div className={classNames(styles.Chat, 'flex flex-col flex-grow min-w-[var(--chat-min-width)] h-full')}>
                {!chatStarted && (
                  <div id="intro" className="mt-[22vh] max-w-chat mx-auto px-6">
                    <h1 className="text-5xl text-center font-bold text-bolt-elements-textPrimary mb-2">
                      Where ideas begin
                    </h1>
                    <p className="mb-4 text-center text-bolt-elements-textSecondary">
                      Bring ideas to life in seconds or get help on existing projects.
                    </p>
                  </div>
                )}
                <div
                  className={classNames('pt-6 px-6', {
                    'h-full flex flex-col': chatStarted,
                  })}
                >
                  <ClientOnly>
                    {() => {
                      return chatStarted ? (
                        <Messages
                          ref={messageRef}
                          className="flex flex-col w-full flex-1 max-w-chat px-4 pb-6 mx-auto z-1"
                          messages={messages}
                          isStreaming={isStreaming}
                          onEdit={onEdit}
                          onResend={onResend}
                          jobEvents={jobEvents}
                          streamingTool={streamingTool}
                          activityEvents={activityEvents}
                          activityStartedAt={activityStartedAt}
                        />
                      ) : null;
                    }}
                  </ClientOnly>
                  <div
                    className={classNames('relative w-full max-w-chat mx-auto z-prompt', {
                      'sticky bottom-0': chatStarted,
                    })}
                  >
                    <div className="galileo-composer">
                      <textarea
                        ref={textareaRef}
                        className="w-full px-4 pt-4 pb-2 focus:outline-none resize-none text-md text-bolt-elements-textPrimary placeholder-bolt-elements-textTertiary bg-transparent"
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') {
                            if (event.shiftKey) {
                              return;
                            }

                            event.preventDefault();

                            sendMessage?.(event);
                          }
                        }}
                        value={input}
                        onChange={(event) => {
                          handleInputChange?.(event);
                        }}
                        style={{
                          minHeight: TEXTAREA_MIN_HEIGHT,
                          maxHeight: TEXTAREA_MAX_HEIGHT,
                        }}
                        placeholder="Ask Galileo to inspect, change, or build…"
                        translate="no"
                      />
                      <div className="galileo-composer-foot">
                        <button className="galileo-composer-btn" disabled title="Attachments arrive with the deploy boundary" type="button">
                          <span className="i-ph:paperclip-tilt text-sm" />
                        </button>
                        <button className="galileo-composer-btn" disabled title="Context files are gathered automatically by the agent" type="button">
                          <span className="i-ph:eye text-sm" />
                          Context
                        </button>
                        <button
                          className={`galileo-composer-btn ${enhancingPrompt ? 'is-on' : ''}`}
                          disabled={input.length === 0 || enhancingPrompt}
                          onClick={() => enhancePrompt?.()}
                          title="Rewrite the prompt with the enhancer"
                          type="button"
                        >
                          {enhancingPrompt ? (
                            <span className="i-svg-spinners:90-ring-with-bg text-sm" />
                          ) : (
                            <span className="i-ph:sparkle text-sm" />
                          )}
                          Enhance
                        </button>
                        <span className="flex-1" />
                        <button
                          aria-label="Toggle auto-enhance"
                          className={`galileo-auto-select ${autoEnhanceOn ? 'is-on' : ''}`}
                          onClick={() => autoEnhance.setKey('enabled', !autoEnhanceOn)}
                          type="button"
                        >
                          Auto
                          <span className={autoEnhanceOn ? 'i-ph:caret-up' : 'i-ph:caret-down'} />
                        </button>
                        <ClientOnly>
                          {() => (
                            <button
                              aria-label={isStreaming ? 'Stop' : 'Send'}
                              className="galileo-send"
                              onClick={(event) => {
                                if (isStreaming) {
                                  handleStop?.();
                                  return;
                                }

                                sendMessage?.(event);
                              }}
                              type="button"
                            >
                              {isStreaming ? (
                                <span className="i-ph:stop-circle-bold text-xl" />
                              ) : (
                                <span className="i-ph:sparkle-fill text-xl" />
                              )}
                            </button>
                          )}
                        </ClientOnly>
                      </div>
                    </div>
                    {input.length > 3 && (
                      <div className="mt-1 text-center text-xs text-bolt-elements-textTertiary">
                        Use <kbd className="kdb">Shift</kbd> + <kbd className="kdb">Return</kbd> for a new line
                      </div>
                    )}
                    <div className="bg-bolt-elements-background-depth-1 pb-4">{/* Ghost Element */}</div>
                  </div>
                </div>
                {!chatStarted && (
                  <div id="examples" className="relative w-full max-w-xl mx-auto mt-8 flex justify-center">
                    <div className="flex flex-col space-y-2 [mask-image:linear-gradient(to_bottom,black_0%,transparent_180%)] hover:[mask-image:none]">
                      {EXAMPLE_PROMPTS.map((examplePrompt, index) => {
                        return (
                          <button
                            key={index}
                            onClick={(event) => {
                              sendMessage?.(event, examplePrompt.text);
                            }}
                            className="group flex items-center w-full gap-2 justify-center bg-transparent text-bolt-elements-textTertiary hover:text-bolt-elements-textPrimary transition-theme"
                          >
                            {examplePrompt.text}
                            <div className="i-ph:arrow-bend-down-left" />
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
              <ClientOnly>{() => <Workbench chatStarted={chatStarted} isStreaming={isStreaming} />}</ClientOnly>
            </div>
          </div>

          <ClientOnly>{() => <InfoRail agentActive={isStreaming} messages={messages} />}</ClientOnly>
        </div>
      </div>
    );
  },
);
