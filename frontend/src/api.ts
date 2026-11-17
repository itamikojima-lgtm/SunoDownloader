export type Format = 'mp3' | 'm4a' | 'wav';
export type Job = {id:string;status:'queued'|'running'|'completed'|'failed'|'cancelled';stage:'queued'|'resolve'|'download'|'rights'|'decrypt'|'convert'|'verify'|'complete';progress:number|null;title:string|null;format:Format;filename:string|null;size_bytes:number|null;error:string|null;download_url:string|null;expires_at:number|null};
export type Health = {status:'ok';ffmpeg:boolean;ffprobe:boolean};
export class ApiError extends Error { constructor(message:string,public status:number){super(message)} }
const record = (v:unknown):v is Record<string,unknown> => typeof v==='object'&&v!==null;
const nullableString = (v:unknown) => v===null||typeof v==='string';
export function parseJob(v:unknown):Job {
 if(!record(v)||typeof v.id!=='string'||!['queued','running','completed','failed','cancelled'].includes(String(v.status))||!['queued','resolve','download','rights','decrypt','convert','verify','complete'].includes(String(v.stage))||!['mp3','m4a','wav'].includes(String(v.format))||![v.title,v.filename,v.error,v.download_url].every(nullableString)||!(v.progress===null||typeof v.progress==='number'&&Number.isFinite(v.progress)&&v.progress>=0&&v.progress<=1)||!(v.size_bytes===null||typeof v.size_bytes==='number'&&v.size_bytes>=0)||!(v.expires_at===null||typeof v.expires_at==='number'))throw new Error('サーバーの応答形式を確認できませんでした。');
 if(v.download_url!==null && !/^\/api\/jobs\/[A-Za-z0-9_-]+\/download$/.test(String(v.download_url)))throw new Error('保存リンクの形式が不正です。');
 return v as Job;
}
export function validUrl(value:string):boolean {try {const u=new URL(value);return u.protocol==='https:'&&['suno.com','www.suno.com'].includes(u.hostname)&&!u.username&&!u.password&&!u.hash&&!u.port&&(/^\/s\/[A-Za-z0-9_-]{4,64}\/?$/.test(u.pathname)||/^\/song\/[0-9a-f-]{36}\/?$/i.test(u.pathname));}catch{return false}}
export async function request(path:string,init:RequestInit={}):Promise<unknown>{
 const abort=new AbortController();const timer=setTimeout(()=>abort.abort(),20000);
 try{const response=await fetch(path,{...init,credentials:'same-origin',signal:abort.signal});const data:unknown=await response.json();if(!response.ok){let message='処理に失敗しました。時間をおいて再試行してください。';if(record(data)&&typeof data.detail==='string')message=data.detail;throw new ApiError(message,response.status)}return data;}catch(error){if(error instanceof ApiError)throw error;if(error instanceof Error&&error.name==='AbortError')throw new Error('接続に時間がかかっています。サーバー起動後に再試行してください。');throw error;}finally{clearTimeout(timer)}
}
let sessionPromise:Promise<string>|null=null;
export function session():Promise<string>{if(!sessionPromise)sessionPromise=request('/api/session').then(v=>{if(!record(v)||typeof v.csrf!=='string')throw new Error('接続を準備できませんでした。');return v.csrf}).catch(error=>{sessionPromise=null;throw error});return sessionPromise}
export async function health():Promise<Health>{const v=await request('/api/health');if(!record(v)||v.status!=='ok'||typeof v.ffmpeg!=='boolean'||typeof v.ffprobe!=='boolean')throw new Error('サーバーの状態を確認できませんでした。');return v as Health}
export async function createJob(url:string,format:Format,csrf:string):Promise<Job>{return parseJob(await request('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({url,format})}))}
export async function getJob(id:string):Promise<Job>{return parseJob(await request('/api/jobs/'+encodeURIComponent(id)))}
export async function cancelJob(id:string,csrf:string):Promise<Job>{return parseJob(await request('/api/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST',headers:{'X-CSRF-Token':csrf}}))}
