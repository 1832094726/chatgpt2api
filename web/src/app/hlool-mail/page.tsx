"use client";

import { useEffect, useMemo, useState } from "react";
import { Copy, Inbox, LoaderCircle, Mail, MailCheck, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  clearHLOOLEmails,
  deleteHLOOLMailbox,
  fetchHLOOLDomains,
  fetchHLOOLEmails,
  fetchHLOOLMailboxes,
  fetchHLOOLNextEmail,
  generateHLOOLMailbox,
  readHLOOLEmail,
  type HLOOLEmailMessage,
  type HLOOLMailbox,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

const STORAGE_KEY = "chatgpt2api_hlool_mail_toolbox";

type SavedConfig = {
  apiBase: string;
  apiKey: string;
};

function extractCode(message: HLOOLEmailMessage | null) {
  if (!message) return "";
  const content = [message.subject, message.preview, message.text_content, message.html_content].filter(Boolean).join("\n");
  return content.match(/\b\d{6}\b/)?.[0] || "";
}

function normalizeDomainList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") return String((item as { domain?: unknown }).domain || "");
      return "";
    })
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatTime(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export default function HLOOLMailPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  const [apiBase, setApiBase] = useState("https://email.hlool.cc");
  const [apiKey, setApiKey] = useState("");
  const [publicDomains, setPublicDomains] = useState<string[]>([]);
  const [privateDomains, setPrivateDomains] = useState<string[]>([]);
  const [domain, setDomain] = useState("");
  const [prefix, setPrefix] = useState("");
  const [query, setQuery] = useState("");
  const [mailboxes, setMailboxes] = useState<HLOOLMailbox[]>([]);
  const [selectedMailbox, setSelectedMailbox] = useState("");
  const [emails, setEmails] = useState<HLOOLEmailMessage[]>([]);
  const [selectedEmail, setSelectedEmail] = useState<HLOOLEmailMessage | null>(null);
  const [isLoadingDomains, setIsLoadingDomains] = useState(false);
  const [isLoadingMailboxes, setIsLoadingMailboxes] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isLoadingEmails, setIsLoadingEmails] = useState(false);

  const code = useMemo(() => extractCode(selectedEmail), [selectedEmail]);
  const allDomains = useMemo(() => [...privateDomains, ...publicDomains], [privateDomains, publicDomains]);

  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}") as Partial<SavedConfig>;
      if (saved.apiBase) setApiBase(saved.apiBase);
      if (saved.apiKey) setApiKey(saved.apiKey);
    } catch {
      // Ignore corrupted local cache.
    }
  }, []);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ apiBase, apiKey }));
  }, [apiBase, apiKey]);

  const basePayload = () => ({ api_base: apiBase.trim(), api_key: apiKey.trim() });

  const ensureApiKey = () => {
    if (!apiKey.trim()) {
      toast.error("请先填写 HLOOL API Key");
      return false;
    }
    return true;
  };

  const loadDomains = async () => {
    if (!ensureApiKey()) return;
    setIsLoadingDomains(true);
    try {
      const res = await fetchHLOOLDomains(basePayload());
      const data = res.data || {};
      const publicItems = normalizeDomainList(data.public_domains || data.domains || []);
      const privateItems = normalizeDomainList(data.private_domains || []);
      setPublicDomains(publicItems);
      setPrivateDomains(privateItems);
      if (!domain && (privateItems[0] || publicItems[0])) {
        setDomain(privateItems[0] || publicItems[0]);
      }
      toast.success(`已获取 ${publicItems.length + privateItems.length} 个域名`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "获取域名失败");
    } finally {
      setIsLoadingDomains(false);
    }
  };

  const loadMailboxes = async () => {
    if (!ensureApiKey()) return;
    setIsLoadingMailboxes(true);
    try {
      const res = await fetchHLOOLMailboxes({ ...basePayload(), page: 1, per_page: 50, q: query.trim() });
      setMailboxes(res.data?.items || []);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "获取邮箱列表失败");
    } finally {
      setIsLoadingMailboxes(false);
    }
  };

  const createMailbox = async () => {
    if (!ensureApiKey()) return;
    setIsGenerating(true);
    try {
      const res = await generateHLOOLMailbox({
        ...basePayload(),
        payload: {
          ...(prefix.trim() ? { prefix: prefix.trim() } : {}),
          ...(domain.trim() ? { domain: domain.trim() } : {}),
        },
      });
      const email = res.data?.email || "";
      if (!email) {
        toast.error(res.error || "邮箱创建失败");
        return;
      }
      setSelectedMailbox(email);
      setPrefix("");
      toast.success(`已创建邮箱 ${email}`);
      await loadMailboxes();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "邮箱创建失败");
    } finally {
      setIsGenerating(false);
    }
  };

  const loadEmails = async (email = selectedMailbox) => {
    if (!ensureApiKey() || !email) return;
    setSelectedMailbox(email);
    setIsLoadingEmails(true);
    try {
      const res = await fetchHLOOLEmails({ ...basePayload(), email, page: 1, per_page: 30 });
      const items = res.data?.items || [];
      setEmails(items);
      setSelectedEmail(items[0] || null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "获取邮件失败");
    } finally {
      setIsLoadingEmails(false);
    }
  };

  const loadNextEmail = async (email: string) => {
    if (!ensureApiKey() || !email) return;
    try {
      const res = await fetchHLOOLNextEmail({ ...basePayload(), email });
      const message = res.data?.message;
      if (res.data?.has_email && message) {
        setSelectedMailbox(email);
        setSelectedEmail(message);
        setEmails((items) => [message, ...items.filter((item) => item.id !== message.id)]);
        toast.success("已获取最新未读邮件");
      } else {
        toast.info("暂无未读邮件");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "检查未读邮件失败");
    }
  };

  const readEmail = async (message: HLOOLEmailMessage) => {
    if (!ensureApiKey()) return;
    try {
      const res = await readHLOOLEmail({ ...basePayload(), id: message.id });
      setSelectedEmail(res.data || message);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取邮件失败");
    }
  };

  const removeMailbox = async (mailbox: HLOOLMailbox) => {
    if (!ensureApiKey()) return;
    if (!window.confirm(`确认删除邮箱 ${mailbox.email}？`)) return;
    try {
      await deleteHLOOLMailbox({ ...basePayload(), id: mailbox.id });
      toast.success("邮箱已删除");
      if (selectedMailbox === mailbox.email) {
        setSelectedMailbox("");
        setEmails([]);
        setSelectedEmail(null);
      }
      await loadMailboxes();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除邮箱失败");
    }
  };

  const clearMailboxEmails = async () => {
    if (!ensureApiKey() || !selectedMailbox) return;
    if (!window.confirm(`确认清空 ${selectedMailbox} 的全部邮件？`)) return;
    try {
      await clearHLOOLEmails({ ...basePayload(), email: selectedMailbox });
      setEmails([]);
      setSelectedEmail(null);
      toast.success("邮件已清空");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清空邮件失败");
    }
  };

  const copyText = async (value: string, label: string) => {
    if (!value) return;
    await navigator.clipboard.writeText(value);
    toast.success(`${label}已复制`);
  };

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return (
    <div className="mx-auto flex min-h-[calc(100vh-49px)] w-full max-w-[1600px] flex-col gap-4 px-4 pt-3 pb-6 md:px-8">
      <section className="border-b border-stone-200 bg-white/80 px-4 py-4 shadow-sm dark:border-white/10 dark:bg-stone-950/70">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <MailCheck className="size-5 text-stone-900 dark:text-stone-100" />
              <h1 className="text-2xl font-semibold tracking-tight">HLOOL Mail 工具箱</h1>
            </div>
            <p className="mt-2 text-sm text-stone-500 dark:text-stone-400">
              通过 HLOOL Mail API 管理临时邮箱、查看邮件并提取验证码。
            </p>
          </div>
          <div className="grid gap-2 md:grid-cols-[220px_minmax(280px,1fr)_auto_auto]">
            <Input value={apiBase} onChange={(event) => setApiBase(event.target.value)} placeholder="API Base" className="h-10 rounded-md bg-white" />
            <Input value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="HLOOL API Key" className="h-10 rounded-md bg-white font-mono text-xs" type="password" />
            <Button type="button" variant="outline" className="h-10 rounded-md" onClick={() => void loadDomains()} disabled={isLoadingDomains}>
              {isLoadingDomains ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
              获取域名
            </Button>
            <Button type="button" className="h-10 rounded-md bg-stone-950 text-white hover:bg-stone-800" onClick={() => void loadMailboxes()} disabled={isLoadingMailboxes}>
              {isLoadingMailboxes ? <LoaderCircle className="size-4 animate-spin" /> : <Inbox className="size-4" />}
              刷新邮箱
            </Button>
          </div>
        </div>
      </section>

      <section className="grid gap-3 lg:grid-cols-[360px_minmax(0,1fr)]">
        <aside className="min-h-[720px] border border-stone-200 bg-white/90 dark:border-white/10 dark:bg-stone-950/75">
          <div className="border-b border-stone-200 p-4 dark:border-white/10">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-sm font-semibold">邮箱创建</h2>
              <Badge variant="outline" className="rounded-md">{allDomains.length} 个域名</Badge>
            </div>
            <div className="mt-3 grid gap-2">
              <Input value={prefix} onChange={(event) => setPrefix(event.target.value)} placeholder="邮箱前缀，可留空" className="h-10 rounded-md bg-white" />
              <select
                value={domain}
                onChange={(event) => setDomain(event.target.value)}
                className="h-10 rounded-md border border-stone-200 bg-white px-3 text-sm outline-none dark:border-white/10 dark:bg-stone-900"
              >
                <option value="">随机域名</option>
                {privateDomains.map((item) => (
                  <option key={`private-${item}`} value={item}>{item}（私有）</option>
                ))}
                {publicDomains.map((item) => (
                  <option key={`public-${item}`} value={item}>{item}</option>
                ))}
              </select>
              <Button type="button" className="h-10 rounded-md bg-stone-950 text-white hover:bg-stone-800" onClick={() => void createMailbox()} disabled={isGenerating}>
                {isGenerating ? <LoaderCircle className="size-4 animate-spin" /> : <Plus className="size-4" />}
                创建邮箱
              </Button>
            </div>
          </div>

          <div className="border-b border-stone-200 p-4 dark:border-white/10">
            <div className="flex gap-2">
              <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索邮箱" className="h-10 rounded-md bg-white" />
              <Button type="button" variant="outline" className="h-10 rounded-md" onClick={() => void loadMailboxes()}>
                <Search className="size-4" />
              </Button>
            </div>
          </div>

          <div className="max-h-[520px] overflow-y-auto">
            {mailboxes.length === 0 ? (
              <div className="p-6 text-sm text-stone-500">暂无邮箱，点击“刷新邮箱”或创建一个新邮箱。</div>
            ) : (
              mailboxes.map((mailbox) => {
                const active = selectedMailbox === mailbox.email;
                return (
                  <div
                    key={`${mailbox.id}-${mailbox.email}`}
                    className={cn("border-b border-stone-100 p-3 transition dark:border-white/10", active ? "bg-stone-950 text-white dark:bg-white dark:text-stone-950" : "bg-white/60 hover:bg-stone-50 dark:bg-transparent dark:hover:bg-white/5")}
                  >
                    <button type="button" className="block w-full text-left" onClick={() => void loadEmails(mailbox.email)}>
                      <div className="truncate font-mono text-xs">{mailbox.email}</div>
                      <div className={cn("mt-1 text-xs", active ? "text-white/70 dark:text-stone-500" : "text-stone-500")}>{formatTime(mailbox.created_at) || mailbox.domain || "邮箱"}</div>
                    </button>
                    <div className="mt-2 flex gap-2">
                      <Button type="button" size="sm" variant="outline" className="h-8 rounded-md bg-white/90 text-stone-700" onClick={() => void loadNextEmail(mailbox.email)}>
                        未读
                      </Button>
                      <Button type="button" size="sm" variant="outline" className="h-8 rounded-md bg-white/90 text-stone-700" onClick={() => void copyText(mailbox.email, "邮箱")}>
                        <Copy className="size-3" />
                      </Button>
                      <Button type="button" size="sm" variant="outline" className="h-8 rounded-md bg-white/90 text-rose-600" onClick={() => void removeMailbox(mailbox)}>
                        <Trash2 className="size-3" />
                      </Button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </aside>

        <main className="grid min-h-[720px] gap-3 xl:grid-cols-[360px_minmax(0,1fr)]">
          <section className="border border-stone-200 bg-white/90 dark:border-white/10 dark:bg-stone-950/75">
            <div className="flex items-center justify-between border-b border-stone-200 p-4 dark:border-white/10">
              <div>
                <h2 className="text-sm font-semibold">邮件列表</h2>
                <p className="mt-1 truncate text-xs text-stone-500">{selectedMailbox || "未选择邮箱"}</p>
              </div>
              <div className="flex gap-2">
                <Button type="button" size="sm" variant="outline" className="h-8 rounded-md" onClick={() => void loadEmails()} disabled={!selectedMailbox || isLoadingEmails}>
                  {isLoadingEmails ? <LoaderCircle className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
                </Button>
                <Button type="button" size="sm" variant="outline" className="h-8 rounded-md text-rose-600" onClick={() => void clearMailboxEmails()} disabled={!selectedMailbox}>
                  清空
                </Button>
              </div>
            </div>
            <div className="max-h-[640px] overflow-y-auto">
              {emails.length === 0 ? (
                <div className="p-6 text-sm text-stone-500">选择邮箱后可查看邮件。</div>
              ) : (
                emails.map((email) => (
                  <button
                    key={email.id}
                    type="button"
                    className={cn("block w-full border-b border-stone-100 p-3 text-left transition dark:border-white/10", selectedEmail?.id === email.id ? "bg-stone-100 dark:bg-white/10" : "hover:bg-stone-50 dark:hover:bg-white/5")}
                    onClick={() => void readEmail(email)}
                  >
                    <div className="flex items-center gap-2">
                      <Mail className="size-4 shrink-0 text-stone-400" />
                      <div className="min-w-0 flex-1 truncate text-sm font-medium">{email.subject || "无主题"}</div>
                    </div>
                    <div className="mt-1 truncate text-xs text-stone-500">{email.from_address || "未知发件人"}</div>
                    <div className="mt-1 text-xs text-stone-400">{formatTime(email.created_at)}</div>
                  </button>
                ))
              )}
            </div>
          </section>

          <section className="border border-stone-200 bg-white/90 dark:border-white/10 dark:bg-stone-950/75">
            <div className="flex flex-col gap-3 border-b border-stone-200 p-4 dark:border-white/10 md:flex-row md:items-start md:justify-between">
              <div>
                <h2 className="text-sm font-semibold">邮件内容</h2>
                <p className="mt-1 text-xs text-stone-500">{selectedEmail?.from_address || "未选择邮件"}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {code ? <Badge variant="success" className="rounded-md font-mono text-sm">验证码 {code}</Badge> : <Badge variant="outline" className="rounded-md">未识别验证码</Badge>}
                <Button type="button" size="sm" variant="outline" className="h-8 rounded-md" onClick={() => void copyText(code, "验证码")} disabled={!code}>
                  <Copy className="size-3" />
                  复制验证码
                </Button>
              </div>
            </div>
            {selectedEmail ? (
              <div className="space-y-4 p-4">
                <div>
                  <div className="text-xs text-stone-500">主题</div>
                  <div className="mt-1 text-lg font-semibold">{selectedEmail.subject || "无主题"}</div>
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  <div className="border border-stone-200 p-3 dark:border-white/10">
                    <div className="text-xs text-stone-500">邮件 ID</div>
                    <div className="mt-1 break-all font-mono text-xs">{selectedEmail.id}</div>
                  </div>
                  <div className="border border-stone-200 p-3 dark:border-white/10">
                    <div className="text-xs text-stone-500">时间</div>
                    <div className="mt-1 text-sm">{formatTime(selectedEmail.created_at) || "未知"}</div>
                  </div>
                </div>
                <Textarea
                  readOnly
                  value={selectedEmail.text_content || selectedEmail.preview || ""}
                  placeholder="没有文本正文"
                  className="min-h-48 rounded-md bg-white font-mono text-xs leading-6"
                />
                <Textarea
                  readOnly
                  value={selectedEmail.html_content || ""}
                  placeholder="没有 HTML 正文"
                  className="min-h-56 rounded-md bg-white font-mono text-xs leading-6"
                />
              </div>
            ) : (
              <div className="flex min-h-[520px] items-center justify-center text-sm text-stone-500">从左侧选择一封邮件查看正文。</div>
            )}
          </section>
        </main>
      </section>
    </div>
  );
}
