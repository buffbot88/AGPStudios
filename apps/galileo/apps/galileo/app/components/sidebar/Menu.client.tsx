import { motion, type Variants } from 'framer-motion';
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'react-toastify';
import { Dialog, DialogButton, DialogDescription, DialogRoot, DialogTitle } from '~/components/ui/Dialog';
import { IconButton } from '~/components/ui/IconButton';
import { ThemeSwitch } from '~/components/ui/ThemeSwitch';
import { db, deleteAll, deleteById, getAll, chatId, type ChatHistoryItem } from '~/lib/persistence';
import { cubicEasingFn } from '~/utils/easings';
import { logger } from '~/utils/logger';
import { HistoryItem } from './HistoryItem';
import { binDates } from './date-binning';

const menuVariants = {
  closed: {
    opacity: 0,
    visibility: 'hidden',
    left: '-150px',
    transition: {
      duration: 0.2,
      ease: cubicEasingFn,
    },
  },
  open: {
    opacity: 1,
    visibility: 'initial',
    left: 0,
    transition: {
      duration: 0.2,
      ease: cubicEasingFn,
    },
  },
} satisfies Variants;

type DialogContent = { type: 'delete'; item: ChatHistoryItem } | { type: 'clear' } | null;

export function Menu() {
  const menuRef = useRef<HTMLDivElement>(null);
  const [list, setList] = useState<ChatHistoryItem[]>([]);
  const [open, setOpen] = useState(false);
  const [dialogContent, setDialogContent] = useState<DialogContent>(null);

  const loadEntries = useCallback(() => {
    getAll(db)
      .then((list) => list.filter((item) => item.urlId))
      .then(setList)
      .catch((error) => toast.error(error.message));
  }, []);

  const deleteItem = useCallback((event: React.UIEvent, item: ChatHistoryItem) => {
    event.preventDefault();

    deleteById(db, item.id)
      .then(() => {
        loadEntries();

        if (chatId.get() === item.id) {
          // hard page navigation to clear the stores
          window.location.pathname = '/';
        }
      })
      .catch((error) => {
        toast.error('Failed to delete conversation');
        logger.error(error);
      });
  }, []);

  const closeDialog = () => {
    setDialogContent(null);
  };

  const clearHistory = useCallback(() => {
    deleteAll(db)
      .then(() => {
        setList([]);
        if (chatId.get()) window.location.pathname = '/';
      })
      .catch((error) => {
        toast.error('Failed to clear chat history');
        logger.error(error);
      });
  }, []);

  useEffect(() => {
    if (open) {
      loadEntries();
    }
  }, [open]);

  useEffect(() => {
    const enterThreshold = 40;
    const exitThreshold = 40;

    function onMouseMove(event: MouseEvent) {
      if (event.pageX < enterThreshold) {
        setOpen(true);
      }

      if (menuRef.current && event.clientX > menuRef.current.getBoundingClientRect().right + exitThreshold) {
        setOpen(false);
      }
    }

    window.addEventListener('mousemove', onMouseMove);

    return () => {
      window.removeEventListener('mousemove', onMouseMove);
    };
  }, []);

  return (
    <>
      <IconButton
        icon={open ? 'i-ph:x' : 'i-ph:list'}
        size="lg"
        title={open ? 'Close chat history' : 'Open chat history'}
        className="fixed left-3 top-3 z-[1000] bg-bolt-elements-background-depth-2/90 shadow-md backdrop-blur-sm"
        onClick={() => setOpen((value) => !value)}
      />
      <motion.div
      ref={menuRef}
      initial="closed"
      animate={open ? 'open' : 'closed'}
      variants={menuVariants}
      className="flex flex-col side-menu fixed top-0 w-[350px] h-full bg-bolt-elements-background-depth-2 border-r rounded-r-3xl border-bolt-elements-borderColor z-sidebar shadow-xl shadow-bolt-elements-sidebar-dropdownShadow text-sm"
    >
      <div className="flex items-center justify-between h-[var(--header-height)] px-4">
        <span className="text-bolt-elements-textPrimary font-medium">Galileo</span>
        <IconButton icon="i-ph:x" size="md" title="Close chat history" onClick={() => setOpen(false)} />
      </div>
      <div className="flex-1 flex flex-col h-full w-full overflow-hidden">
        <div className="p-4">
          <a
            href="/"
            className="flex gap-2 items-center bg-bolt-elements-sidebar-buttonBackgroundDefault text-bolt-elements-sidebar-buttonText hover:bg-bolt-elements-sidebar-buttonBackgroundHover rounded-md p-2 transition-theme"
          >
            <span className="inline-block i-bolt:chat scale-110" />
            Start new chat
          </a>
        </div>
        <div className="text-bolt-elements-textPrimary font-medium pl-6 pr-5 my-2">Your Chats</div>
        <div className="flex-1 overflow-scroll pl-4 pr-5 pb-5">
          {list.length === 0 && <div className="pl-2 text-bolt-elements-textTertiary">No previous conversations</div>}
          <DialogRoot open={dialogContent !== null}>
            {binDates(list).map(({ category, items }) => (
              <div key={category} className="mt-4 first:mt-0 space-y-1">
                <div className="text-bolt-elements-textTertiary sticky top-0 z-1 bg-bolt-elements-background-depth-2 pl-2 pt-2 pb-1">
                  {category}
                </div>
                {items.map((item) => (
                  <HistoryItem key={item.id} item={item} onDelete={() => setDialogContent({ type: 'delete', item })} />
                ))}
              </div>
            ))}
            <Dialog onBackdrop={closeDialog} onClose={closeDialog}>
              {dialogContent?.type === 'delete' && (
                <>
                  <DialogTitle>Delete Chat?</DialogTitle>
                  <DialogDescription asChild>
                    <div>
                      <p>
                        You are about to delete <strong>{dialogContent.item.description}</strong>.
                      </p>
                      <p className="mt-1">Are you sure you want to delete this chat?</p>
                    </div>
                  </DialogDescription>
                  <div className="px-5 pb-4 bg-bolt-elements-background-depth-2 flex gap-2 justify-end">
                    <DialogButton type="secondary" onClick={closeDialog}>
                      Cancel
                    </DialogButton>
                    <DialogButton
                      type="danger"
                      onClick={(event) => {
                        deleteItem(event, dialogContent.item);
                        closeDialog();
                      }}
                    >
                      Delete
                    </DialogButton>
                  </div>
                </>
              )}
              {dialogContent?.type === 'clear' && (
                <>
                  <DialogTitle>Clear chat history?</DialogTitle>
                  <DialogDescription>This deletes all saved conversations from this browser.</DialogDescription>
                  <div className="px-5 pb-4 bg-bolt-elements-background-depth-2 flex gap-2 justify-end">
                    <DialogButton type="secondary" onClick={closeDialog}>Cancel</DialogButton>
                    <DialogButton type="danger" onClick={() => { clearHistory(); closeDialog(); }}>Clear all</DialogButton>
                  </div>
                </>
              )}
            </Dialog>
          </DialogRoot>
        </div>
        <div className="flex items-center gap-2 border-t border-bolt-elements-borderColor p-4">
          <button
            className="text-bolt-elements-textTertiary hover:text-bolt-elements-item-contentDanger transition-colors"
            onClick={() => setDialogContent({ type: 'clear' })}
          >
            Clear history
          </button>
          <ThemeSwitch className="ml-auto" />
        </div>
      </div>
      </motion.div>
    </>
  );
}
