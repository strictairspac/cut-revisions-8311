/** @typedef {{id:number, title:string, payload:object, author:string}} BusinessCase */
/** @typedef {{id:number, case_id:number, status:'queued'|'running'|'complete'|'failed'}} Run */
/** @typedef {{id:number, run_id:number, digest:string, report:{summary:string, metrics:object, sections:Array}}} Evidence */
/** @typedef {{id:number, status:'building'|'ready', snapshot_id:string, counts:object, digest:string,
 *             created_by:string, created_at:string, finished_at:string}} HandoverSheet */
/** @typedef {{sheet:HandoverSheet, section:string, page:number, pages:number, page_size:number,
 *             total:number, rows:Array<object>}} HandoverPage */
