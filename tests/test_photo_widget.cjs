
const {readFileSync}=require("node:fs");
const {runInNewContext}=require("node:vm");
const a=require("node:assert/strict");
const {join} = require("node:path");
const html = readFileSync(join(__dirname, "..", "telegram_reader", "photo_widget.html"), "utf8");
const calls=[]; const parent={postMessage:m=>calls.push(m)};
let handler,draws=0;
const photo={hidden:true,getContext:()=>({drawImage:()=>draws++})},status={};
runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],{
 window:{parent},document:{getElementById:id=>id==="photo"?photo:status},
 addEventListener:(type,fn)=>{if(type==="message")handler=fn},atob,Uint8Array,Blob,
 createImageBitmap:async blob=>{a.equal(blob.type,"image/jpeg");return {width:640,height:480,close(){}}}
});
const msg=(source,params)=>handler({source,data:{jsonrpc:"2.0",method:"ui/notifications/tool-result",params}});
(async()=>{
 a.equal(calls[0].method,"ui/initialize");
 handler({source:parent,data:{jsonrpc:"2.0",id:1,result:{}}});
 a.equal(calls[1].method,"ui/notifications/initialized");
 msg({}, {_meta:{telegramPhotoJpeg:"/9j/xA=="}});await new Promise(setImmediate);a.equal(draws,0);
 msg(parent,{_meta:{telegramPhotoJpeg:"/9j/xA=="}});await new Promise(setImmediate);
 a.equal(draws,1);a.equal(photo.hidden,false);a.equal(photo.width,640);
 msg(parent,{isError:true});a.equal(photo.hidden,true);a.equal(photo.width,0);
 console.log("Widget bridge, rendering and denial tests passed");
})().catch(e=>{console.error(e);process.exitCode=1});
