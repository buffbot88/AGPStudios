import { useState } from 'react';
import { toast } from 'react-toastify';
import { friendlyChatErrorMessage } from '~/utils/chat-errors';
import { createScopedLogger } from '~/utils/logger';

const logger = createScopedLogger('usePromptEnhancement');

/**
 * Rewrites a prompt through the enhancer endpoint. Returns null on failure
 * (the caller decides whether to fall back to the original text).
 */
export async function enhanceText(message: string): Promise<string | null> {
  const response = await fetch('/api/enhancer', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
    }),
  });

  if (!response.ok) {
    const detail = (await response.json().catch(() => null)) as Parameters<typeof friendlyChatErrorMessage>[0];

    logger.error('Enhancer request failed', detail);
    toast.error(friendlyChatErrorMessage(detail), { autoClose: 8000 });

    return null;
  }

  return response.text();
}

export function usePromptEnhancer() {
  const [enhancingPrompt, setEnhancingPrompt] = useState(false);
  const [promptEnhanced, setPromptEnhanced] = useState(false);

  const resetEnhancer = () => {
    setEnhancingPrompt(false);
    setPromptEnhanced(false);
  };

  const enhancePrompt = async (input: string, setInput: (value: string) => void) => {
    if (!input.trim()) return;
    setEnhancingPrompt(true);
    setPromptEnhanced(false);

    try {
      const enhanced = await enhanceText(input);

      if (enhanced === null) {
        setEnhancingPrompt(false);

        return;
      }

      setInput(enhanced);
      setEnhancingPrompt(false);
      setPromptEnhanced(true);
    } catch (error) {
      logger.error(error);
      toast.error(friendlyChatErrorMessage(error), { autoClose: 8000 });
      setEnhancingPrompt(false);
    }
  };

  return { enhancingPrompt, promptEnhanced, enhancePrompt, resetEnhancer };
}
