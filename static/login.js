/*==================================================
 CCTS LIVE MAP
 LOGIN.JS
==================================================*/

document.addEventListener("DOMContentLoaded",()=>{

initClock();

initParticles();

initStations();

initPassword();

initLogin();

initDashboard();

});


/*==================================================
 CLOCK
==================================================*/

function initClock(){

const clock=document.getElementById("clock");

if(!clock) return;

function update(){

const d=new Date();

clock.innerHTML=d.toLocaleTimeString("vi-VN",{

hour12:false

});

}

update();

setInterval(update,1000);

}


/*==================================================
 PASSWORD
==================================================*/

function initPassword(){

const btn=document.querySelector(".toggle-password");

const input=document.getElementById("password");

if(!btn||!input) return;

btn.innerHTML="👁";

btn.onclick=()=>{

if(input.type==="password"){

input.type="text";

btn.innerHTML="🙈";

}else{

input.type="password";

btn.innerHTML="👁";

}

}

}


/*==================================================
 LOGIN
==================================================*/

function initLogin(){

const form=document.getElementById("loginForm");

if(!form) return;

const button=form.querySelector(".login-btn");

form.addEventListener("submit",()=>{

button.classList.add("loading");

button.innerHTML="VERIFYING...";

});

}


/*==================================================
 DASHBOARD COUNTER
==================================================*/

function initDashboard(){

document

.querySelectorAll(".info-card .value")

.forEach(el=>{

const target=parseInt(

el.innerText.replace(/,/g,"")

);

let value=0;

const step=Math.max(

1,

Math.floor(target/80)

);

const timer=setInterval(()=>{

value+=step;

if(value>=target){

value=target;

clearInterval(timer);

}

el.innerText=value.toLocaleString();

},20);

});

}


/*==================================================
 PARTICLES
==================================================*/

function initParticles(){

const panel=document.querySelector(".left-panel");

if(!panel) return;

for(let i=0;i<35;i++){

const p=document.createElement("div");

p.className="particle";

p.style.left=Math.random()*100+"%";

p.style.top=Math.random()*100+"%";

p.style.animationDuration=

8+Math.random()*8+"s";

p.style.animationDelay=

Math.random()*6+"s";

panel.appendChild(p);

}

}


/*==================================================
 STATIONS
==================================================*/

function initStations(){

const layer=document.querySelector(".station-layer");

if(!layer) return;

const points=[

[18,18],

[22,24],

[30,33],

[34,42],

[37,49],

[40,56],

[45,62],

[48,68],

[53,74],

[58,82],

[61,88],

[66,95],

[73,42],

[69,60],

[63,30],

[52,26],

[44,18],

[55,48],

[46,40],

[39,28],

[31,20]

];

points.forEach((p,index)=>{

const node=document.createElement("div");

node.className="station";

if(index%9===0){

node.classList.add("warning");

}

if(index%13===0){

node.classList.add("error");

}

node.style.left=p[0]+"%";

node.style.top=p[1]+"%";

node.style.animationDelay=

Math.random()*2+"s";

layer.appendChild(node);

});

drawNetwork(points);

}


/*==================================================
 NETWORK
==================================================*/

function drawNetwork(points){

const canvas=document.getElementById("networkCanvas");

if(!canvas) return;

const ctx=canvas.getContext("2d");

function resize(){

canvas.width=canvas.offsetWidth;

canvas.height=canvas.offsetHeight;

render();

}

window.addEventListener("resize",resize);

resize();

function render(){

ctx.clearRect(0,0,canvas.width,canvas.height);

ctx.strokeStyle="rgba(0,212,255,.25)";

ctx.lineWidth=1.2;

for(let i=0;i<points.length-1;i++){

const a=points[i];

const b=points[i+1];

ctx.beginPath();

ctx.moveTo(

canvas.width*a[0]/100,

canvas.height*a[1]/100

);

ctx.lineTo(

canvas.width*b[0]/100,

canvas.height*b[1]/100

);

ctx.stroke();

}

}

}


/*==================================================
 RANDOM BLINK
==================================================*/

setInterval(()=>{

const nodes=

document.querySelectorAll(".station");

if(nodes.length===0) return;

const n=

nodes[Math.floor(

Math.random()*nodes.length

)];

n.style.transform="scale(2)";

setTimeout(()=>{

n.style.transform="";

},400);

},700);


/*==================================================
 CARD HOVER
==================================================*/

document

.querySelectorAll(".info-card")

.forEach(card=>{

card.addEventListener("mousemove",e=>{

const r=card.getBoundingClientRect();

const x=e.clientX-r.left;

const y=e.clientY-r.top;

card.style.background=

`radial-gradient(circle at ${x}px ${y}px,
rgba(0,212,255,.12),
rgba(255,255,255,.04))`;

});

card.addEventListener("mouseleave",()=>{

card.style.background="";

});

});


/*==================================================
 PARALLAX
==================================================*/

document.addEventListener("mousemove",e=>{

const glow1=document.querySelector(".glow-1");

const glow2=document.querySelector(".glow-2");

if(!glow1||!glow2) return;

const x=e.clientX/window.innerWidth;

const y=e.clientY/window.innerHeight;

glow1.style.transform=

`translate(${x*25}px,${y*25}px)`;

glow2.style.transform=

`translate(${-x*30}px,${-y*30}px)`;

});


/*==================================================
 RIPPLE BUTTON
==================================================*/

const loginButton=document.querySelector(".login-btn");

if(loginButton){

loginButton.addEventListener("click",function(e){

const ripple=document.createElement("span");

const rect=this.getBoundingClientRect();

const size=Math.max(rect.width,rect.height);

ripple.style.position="absolute";
ripple.style.width=size+"px";
ripple.style.height=size+"px";
ripple.style.left=e.clientX-rect.left-size/2+"px";
ripple.style.top=e.clientY-rect.top-size/2+"px";
ripple.style.borderRadius="50%";
ripple.style.background="rgba(255,255,255,.35)";
ripple.style.transform="scale(0)";
ripple.style.pointerEvents="none";
ripple.style.transition=".6s";

this.appendChild(ripple);

requestAnimationFrame(()=>{
ripple.style.transform="scale(3)";
ripple.style.opacity="0";
});

setTimeout(()=>{

ripple.remove();

},600);

});

}

console.log("CCTS LIVE MAP UI Loaded");