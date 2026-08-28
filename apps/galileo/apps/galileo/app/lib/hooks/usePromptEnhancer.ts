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
    setEnhancingPrompt(true);
    setPromptEnhanced(false);

    const originalInput = input;

    try {
      const response = await fetch('/api/enhancer', {
        method: 'POST',
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

      const reader = response.body?.getReader();

      if (!reader) {
        setEnhancingPrompt(false);

        return;
      }

      const decoder = new TextDecoder();

      let _input = '';
      let _error;

      try {
        setInput('');

        while (true) {
          const { value, done } = await reader.read();

          if (done) {
            break;
          }

          _input += decoder.decode(value);

          logger.trace('Set input', _input);

          setInput(_input);
        }
      } catch (error) {
        _error = error;
        setInput(originalInput);
      } finally {
        if (_error) {
          logger.error(_error);
          toast.error(friendlyChatErrorMessage(_error), { autoClose: 8000 });
        }

        setEnhancingPrompt(false);
        setPromptEnhanced(true);

        setTimeout(() => {
          setInput(_input);
        });
      }
    } catch (error) {
      logger.error(error);
      toast.error(friendlyChatErrorMessage(error), { autoClose: 8000 });
      setEnhancingPrompt(false);
    }
  };

  return { enhancingPrompt, promptEnhanced, enhancePrompt, resetEnhancer };
}
