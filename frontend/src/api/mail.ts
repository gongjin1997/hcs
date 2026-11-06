import { get, post, put } from './request'
import type { MailConnectionResult, MailSettings, MailSettingsUpdate, TestMailResult } from '@/types/mail'

export const getMailSettings = () => get<MailSettings>('/notify/mail-settings')
export const saveMailSettings = (body: MailSettingsUpdate) => put<MailSettings>('/notify/mail-settings', body)
// 单次连接包括 TLS / 认证阶段；界面独立按钮触发，不在加载或保存时执行。
const testConfig = { timeout: 60000, silent: true }
export const testSmtpConnection = () => post<MailConnectionResult>('/notify/test-smtp', undefined, testConfig)
export const testImapConnection = () => post<MailConnectionResult>('/notify/test-imap', undefined, testConfig)
export const sendMailTest = (user_ids: number[]) => post<TestMailResult>('/notify/test-mail', { user_ids }, testConfig)
