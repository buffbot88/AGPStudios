import type { Message } from 'ai';
import { createScopedLogger } from '~/utils/logger';
import type { ChatHistoryItem } from './useChatHistory';

const logger = createScopedLogger('ChatHistory');
const LOCAL_KEY = 'galileo_chats';

function localChats(): ChatHistoryItem[] {
  try {
    return JSON.parse(localStorage.getItem(LOCAL_KEY) || '[]') as ChatHistoryItem[];
  } catch {
    return [];
  }
}

function saveLocalChats(chats: ChatHistoryItem[]) {
  try {
    localStorage.setItem(LOCAL_KEY, JSON.stringify(chats));
  } catch (error) {
    logger.error('Unable to save chats to localStorage', error);
  }
}

// this is used at the top level and never rejects
export async function openDatabase(): Promise<IDBDatabase | undefined> {
  return new Promise((resolve) => {
    const request = indexedDB.open('boltHistory', 1);

    request.onupgradeneeded = (event: IDBVersionChangeEvent) => {
      const db = (event.target as IDBOpenDBRequest).result;

      if (!db.objectStoreNames.contains('chats')) {
        const store = db.createObjectStore('chats', { keyPath: 'id' });
        store.createIndex('id', 'id', { unique: true });
        store.createIndex('urlId', 'urlId', { unique: true });
      }
    };

    request.onsuccess = (event: Event) => {
      resolve((event.target as IDBOpenDBRequest).result);
    };

    request.onerror = (event: Event) => {
      resolve(undefined);
      logger.error((event.target as IDBOpenDBRequest).error);
    };
  });
}

export async function getAll(db: IDBDatabase | undefined): Promise<ChatHistoryItem[]> {
  const local = localChats();
  if (!db) return local.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('chats', 'readonly');
    const store = transaction.objectStore('chats');
    const request = store.getAll();

    request.onsuccess = () => {
      const merged = new Map<string, ChatHistoryItem>();
      [...(request.result as ChatHistoryItem[]), ...local].forEach((item) => merged.set(item.id, item));
      resolve([...merged.values()].sort((a, b) => b.timestamp.localeCompare(a.timestamp)));
    };
    request.onerror = () => reject(request.error);
  });
}

export async function setMessages(
  db: IDBDatabase | undefined,
  id: string,
  messages: Message[],
  urlId?: string,
  description?: string,
): Promise<void> {
  const item = { id, messages, urlId, description, timestamp: new Date().toISOString() };
  saveLocalChats([...localChats().filter((chat) => chat.id !== id), item]);
  if (!db) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('chats', 'readwrite');
    const store = transaction.objectStore('chats');

    const request = store.put(item);

    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}

export async function getMessages(db: IDBDatabase | undefined, id: string): Promise<ChatHistoryItem | undefined> {
  const local = localChats().find((item) => item.id === id || item.urlId === id);
  return local || (db ? (await getMessagesById(db, id)) || (await getMessagesByUrlId(db, id)) : undefined);
}

export async function getMessagesByUrlId(db: IDBDatabase, id: string): Promise<ChatHistoryItem> {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('chats', 'readonly');
    const store = transaction.objectStore('chats');
    const index = store.index('urlId');
    const request = index.get(id);

    request.onsuccess = () => resolve(request.result as ChatHistoryItem);
    request.onerror = () => reject(request.error);
  });
}

export async function getMessagesById(db: IDBDatabase, id: string): Promise<ChatHistoryItem> {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('chats', 'readonly');
    const store = transaction.objectStore('chats');
    const request = store.get(id);

    request.onsuccess = () => resolve(request.result as ChatHistoryItem);
    request.onerror = () => reject(request.error);
  });
}

export async function deleteById(db: IDBDatabase | undefined, id: string): Promise<void> {
  saveLocalChats(localChats().filter((chat) => chat.id !== id));
  if (!db) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('chats', 'readwrite');
    const store = transaction.objectStore('chats');
    const request = store.delete(id);

    request.onsuccess = () => resolve(undefined);
    request.onerror = () => reject(request.error);
  });
}

export async function getNextId(db: IDBDatabase | undefined): Promise<string> {
  if (!db) {
    const highestId = localChats().reduce((max, chat) => Math.max(max, Number(chat.id) || 0), 0);
    return String(highestId + 1);
  }
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('chats', 'readonly');
    const store = transaction.objectStore('chats');
    const request = store.getAllKeys();

    request.onsuccess = () => {
      const highestId = request.result.reduce((cur, acc) => Math.max(+cur, +acc), 0);
      resolve(String(+highestId + 1));
    };

    request.onerror = () => reject(request.error);
  });
}

export async function getUrlId(db: IDBDatabase | undefined, id: string): Promise<string> {
  const idList = await getUrlIds(db);

  if (!idList.includes(id)) {
    return id;
  } else {
    let i = 2;

    while (idList.includes(`${id}-${i}`)) {
      i++;
    }

    return `${id}-${i}`;
  }
}

async function getUrlIds(db: IDBDatabase | undefined): Promise<string[]> {
  if (!db) return localChats().map((chat) => chat.urlId).filter((id): id is string => Boolean(id));
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('chats', 'readonly');
    const store = transaction.objectStore('chats');
    const idList: string[] = [];

    const request = store.openCursor();

    request.onsuccess = (event: Event) => {
      const cursor = (event.target as IDBRequest<IDBCursorWithValue>).result;

      if (cursor) {
        idList.push(cursor.value.urlId);
        cursor.continue();
      } else {
        resolve(idList);
      }
    };

    request.onerror = () => {
      reject(request.error);
    };
  });
}
