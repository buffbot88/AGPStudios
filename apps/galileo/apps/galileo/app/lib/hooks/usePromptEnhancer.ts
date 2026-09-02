import { useState } from 'react';
import { toast } from 'react-toastify';
import { friendlyChatErrorMessage } from '~/utils/chat-errors';
import { createScopedLogger } from '~/utils/logger';

const logger = createScopedLogger('usePromptEnhancement');

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
      const response = await fetch('/api/enhancer', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: input,
        }),
      });

      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as Parameters<typeof friendlyChatErrorMessage>[0];

        logger.error('Enhancer request failed', detail);
        toast.error(friendlyChatErrorMessage(detail), { autoClose: 8000 });
        setEnhancingPrompt(false);

        return;
      }

      const enhanced = await response.text();
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
