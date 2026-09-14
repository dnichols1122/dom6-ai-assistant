"""Client-serialized pretender chassis payloads for Dominions 6.36.

Generated from the 6.36 Linux client's own static-type and live-unit writers.
The compressed bank is deliberately version-bound; see
knowledge/reverse/pretender_builder.md.
"""
from __future__ import annotations

import base64
import struct
import zlib
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True)
class ChassisPayload:
    chassis_id: int
    serialized_type_id: int
    static_block: bytes
    live_block: bytes


_COMPRESSED_BANK = b"""c-
q~ad7M<$mB;JVLap5mM39~L>NWQjue$GuLIZE1WYMTmuQ4%U6AT&$aOr3gL8GE9%BBKQ6GcGOOeQnQB#tCe6SwH7Gvn@L)FcyEP@
?RhZ0ej6W0H@Tb94FR<a0k&pHF@G!|LjOzpv+>d(J)gobwWi#5<|Pr(fv<|9l9q^AfW@`ZtldH2LWdUYx6b{9iwL9rz)dR}YU>Dx
&@;e<*n+{?4yY{bu;Ptfc0b%!c<S1%ug%F$ja>b6sT&W)nSi$&Mr25t~efko==W)?fc6i{ZUl0by}s00LooE+d1mnCPkd*)@;VxW
l;EY%GWaZ%gM(R>6CV0>rAs`3Q)$St*EBL{D94^H|m4VvE6RQr#9T9S*DEy;VVBHEIq`a&uTs^we2atJzgv=8@QHDv0y}i_o%%HS
pf10I?>q)6#YhYlxmY8F|d9GO^8I3W=np_a%41d%J?dU8p&nAYYX3qVf)2%=}id-DJ=3$Cc@?eXWD{4h4jD@SO*Tuu%?S9nn+gT|
e>$`#cK$SnM#{J?No$0p2?mC|*c-
2#RlIb21b!P_0S<*n=x}8fy>QQ9~l}BD{AgAiS0+AQ1Y??cp`5MQQ1t*48pBvsmmhK6cP}?S%Kc3J^O{5N{=<Aa)WxbuZiXt2`F(
no7a4^uJ^myx&t$*o9KqD5tQC=&3C`^u&9{BG3+@!3f~J`(OyJIPgo}MG<WNG=dL4`XQohlJ7Fx5xWhCpoL*Ky!R>~>_#EHk*LTZ
>?Yf*27%j|vu&~0U=G?V(jIv4SFA^S5_yEfo}_d=+C!Dr17qM~zuC5+hhZ<g-
&Zi$i*J!EDTBR4Pu=Xg(MaYv;(c>5=sV|q^%X>cVIPWNo7^?;BU_R<gZ<G5M%#iWhCF=!Lj{DQg+aJq9xfCuA_%O5j|?JDd}weML
pxk3SbviIU>Hg$hS%g6N<<he9uH=s{^uE-Nz&ucRQKl!22Iwj2!lbnyo^B;l_`Y2)7dYy-I^~fgGp$+hGzJIq=G~<Y7KvK-
+%F!H4~A@xtv9gFOum7irW}`I;Eh%tWyyRPh~4I3XF(CiPt=z#ayJ)1{VV8E=xzlPoxzXj<y`M5TLIN!_h<-
dITb!ZlDu}IiOQU0bq`WRbup%pL)(A0+5K?f=FZ<iNO^hRTl+=E3Byq16O|Fc?A)Jfg$%q7lYR!?bFUH;qzGqf-
9|Q2!f?@1XmIv$nwy$W05r)f~HTN3!ly@Ak0PWU|m)UVJ;N}JBGC=XDoxHe#krzKA%@Gm}gDIsw3qL<`FUI60u0+O*R8v1oPqZ1q
Fins2LnCUyJ5bg=&5%3dR!nC_vMD@+I)`qJqE@t2;vAEV&UZAtI3B9v8(%+7m4YrAi6}E3B>vf^S$&G6XBAqGUggSec1ZBSpzC!l
#=Q6kfE>LMVJ+p1FIGDmtjyKKO&Cw852lx^*w>L91p3g!LA7s#cSq=&YwQ1=|lj#znK?rl4gD8{oq&3JM#n&ma`GB&0rJ0~H0%Yo
aCHK*#($;Imy72zFRsL=cS09VSDtgKA^sv5{PKHFs;IpT6D+pYNt%u+zfIAfNmso%a`i*-
jz`z5R0FR7JN&GK5|5+3p8}aK*<ZT%RPb%c)OeSe$H<)w@`K_6z=|uPSpyce8^ut;-^b_u<2bDeT~V)G2RFd{$-$?-
SWUpOAM1@D_)eYYF;~UIt-%C@_?ha}f-
En%iU;%1N519uRPkxj`6+9wzTmHKVlz1G##tj?<EO1zsdQP9x%w^xbyb5j{<I3%ceo9<(}K0bzXdGz7v5`B~0*A_&c)=Q~^+Zmwg
{Fa4SbTJ=&un3%*8s6Uj4%oB+q^zeg7^fDJgQriwDfl|E{2qvKj)@6Ik5KJOM(AVXz-
6nb)ezLFSpeKV?eH0KTqY#3e6vAXW2yMJAV4=&sfQQ3i$J!@t_Wfq9VvEz^vwamnPDg?KC?}2FPA3A=o4ZW(H5h^J`DS7Y{6s$mf
+@*+5CrSxzF`Uxf~?=hLoWK6YzLNR$<b8!bbkeesYxuSePb?`L6}MeA;(<~tC27I8yhiL`a9M$K&=4^3}+-
;5e)Zar3I;HP(=)3P#(d>0Ao+t4&esjJkV;O>a;&O8i63PQlBu72tv{4p*P5nLNU<n(@wqXQ}f}6Yz2n}D2E&692O99=*P+|^n`6
T1ign}3LkeA5SAv#BM=6(OprlXN(3P_z2XJKg=2Ksp!w|Az~{>f2-
hS_2!w<sbqm)JLCCN$7z80S6Xixa`M(b2aupn|OO8Z1%$AqHTt~#AKP#8HBV2RcvwlKv8EDn2fUqp-
A`pHrj~A9vCG={x>)N8#=+yR~0_eZ$wH&k>q=2wI`8WdMj$Bp-
VL1_mQp~Dg34@HTfzdqdxdF6d3J5nOHy{wEX1mEC+&~pA@Hm*(!69Z4%;ZgyZqQH!%kdN-im4kB5bx!rx7Ug(A`mQQj_?}kE@vA^
RZ%c#OJSKjTYk>dM#P|K2QF*#>U~ElW~Ws3P8%k`4-Hm;n2>5lK#aGfIqnHWAo}{l+IZ-
)n%&05;Iz@Lko5LTQ$R6am0C?<^}>NXZkR%ZqKP?N_-WHSk7z7R(?O}K0>ShYwp)EyzFVD6gun{o&Lnq2FA!DZvq>7S8K7620>q3
IR*dm_V!RB*3?dLcdF-&x9-N7`bOW^?nF(^WE6Vg{rdkjV4}Q93<Od)95K&f-
yJg#{*8{X0Z4a6p7J*zxDC}WT3hS?ZWwxix9u`r#Y0jo~1b%0gPpdL<gu&%8+BJLkfn-AzEbc@1uII@wx7|l(6CQWD!^9AyuX$=3
#r>exkqQj=r`nJn+mFagWbUUj3D+JK_)fhv1O6398lBwGjOZQ$&5lyQcqoM(JbXvKae9afhUZ6hFL9K~#TmNE)+gbIhAJpLnZi;`
&&WH%JV~`{vO~|dJ=>}JVrV1fn4SZ{hABuqm-;zEqMJOw@f;C}F6H*vj>Is-lT2F9`gzc*LjmFW)K3uzZ^{>?=ZPSsc&lIK3^uT$
!)R;Jbk+6dXTudBtn`s+p^S9LBy9)xH+YqJ*^jv+h8tb3q?z}xH$NMpppZ%rLt{DVDNu@tLV>w{RL{7MFg&%PWemr`!;Dm5I4+G{
0bM4)0(u+~h9Vo;!D}&Z_xMOIMmAEHjN{>90tJiX(-ZZ3CLK@2!eUN^yCN|8KCbS<bOJm~sDN-
n8oTpzMpD`*;{+lIed4MJjlS@sX$Vik!$hjMA$<wf={YMU@FWp|Y|Q;=r0-T@5t-arRd+#p3LYj-
|C#a<5Kp1&QEz$J@DvpYzvk5^B|+#Ao4h9JmPkAe4>L+(5l^S*BOrb)4;`K+0@1Iv!*#hBW%m6my`9oC@Gvz6glE#&HHWXtt>GCe
@9f9*oKnr`3qP8fjA!9tj#fZ;Ha!D@FjNlVSt1C1V-`ZpaJ11!4w`-;%p?+{6%fJ<R<d-uJZ7kWF$shyaD3*A(MDgp(#&H-
Al4WKg(&kmgu?D@Nk$<eqLB67sy{gPtFS1IF*$D2O?1Z~*jNRLID^H|{pI!$6Ol;QFM^IW{0^elHfa<nHBNzGR0eDDxm><O8byR4
6NEwRa51itymJl2Iz~aEhElj%?wxBy6q;*ZY*$(6i(^c06`+ISXprkz)ewLTmczJEz91b<#3997Bd^TFv5j=|YBY%TSp|jB87yE}
A+M4dO+;ZJpVsDa@!3Y|9y2C$AjgeC7?YWeKzKCUPX=KO)sj@M*Tx)Yayf=>n}%_j1G#OC!8mlM+Dq;h#t|_nGRH3aRbL!$c)t>@
73rAFf$TPh;h4-?1j7%q($O%-
keNc6h2jLW&z|+xqlqBZ=TyzCGT6QJ2jzF)CK53y1b*z=6)rw!cHbC14wFEw6IBhZG86S{F(wgV=;z10#v*Z|*+HA0cb*Jlouuk$
g;ID%eg|<f5rrg=L)O8?N#;_}uLU|C#5!5k#tNnI%WR9ZjTP0B<g_wzawAnrP64Ha>aG>KL1H<nAxt4c&?WLh9t+V(k-
{|4>J$ZpX_-
?H2sdY?7jdVN<x`zXy>ZVerr!Y70dXdX^?3z_Gf@hOob>SjOsdt0g|RO_Z+HQQmMu&NsZLehJIr7Ok|HUsLz+&+pik)3cSl~x%Hm
XGNgP6%?j<1D7ZfBe$*e<2>}ip9<GzGypXvqf^vL5C?r`x1gOeEA*}UsPu`eo6T%Tz}P;|GBm7%zv>X3(Z*j|0U?H7%|mag&Y-
6fGYO@X0z7wl@!Wpe*e|6&p^J!t8kcqEUj;xvPOgm%-
Gc982!3J&dEu%n(@P8v0|6LDy=9pCdIfD>P8q=Zrj$n|9fhmJ1TyoRej>8J4*e_00+hk<^3yX&{dK5zA7@#RJuL_a;7NQ_tQo3dE
<lCN0DOZQD#q6BN`@_>u+Mh`I2w1}CY)dU5EnOUr92DhYP+)N?}Y2R_$Y)?!udyI^pF`NTpO;k`gCyTl1XXIV2&LN^u0t#ZH*+l?
)PWl2+YLWuM1)mhk%Mk3aiZTQj5FtqV)pjw-
^aegUc2LeG5|dTKlyX>~j8*akcm0b=976rXE#&1m5R(nhDrvQxoB(>AuIek6TZ({qLVo3Q0uhK}5Qaf4PB(XZq~CpYB4{;5b@rLN
7=iHHgADQ^f?zQx;_X~aF}-
(>4v3pUtf>kLH|KgH6c%QswP80CQ7HK#kHl1?EkQGUUI|j2p<u8w7okb+!(|Ls5;4dJ5sy4p@x>WN&tBDBl2(CSUr}&amBV_YceA
7wF{_9;r2W_r#aGPUBk38#YLIH0g2C!s5xd)#kS6q26EW!TR~W<!(;8{e;Tq8DOa+8BIjq6qTZt285Y|u`g2&y#+!1FQ-
|ti7buWl@mV&~)xxHwus<b%lULp!9pAU{~ry|ZWyg$9x(c1%{)>jo69>|SGuk%P(qz8yFbg2f8_-
Z3n0TlCz#M!E*==l>61atEJrA^WEG*8{zukl!%ZFs*bEkh`QRR8#KW7UH(Xv!asFc_7TUSDV;Vvw|*kjJ+8$3v#D-
=e0Vea_YlVog_2Xvt$+)KldYT8Jp5A!rVKG5wGsyiW~5KXY*jXf;DshLOiosi#=dOC*<2nL_)BdI81^lTZG2gS9!J)l7vY%*jtg-
Lf=&G=~U6mRJ2q%+zI&{5vr?P_eD|+vb8)=O_$eZXP?<$;f9A&n03|vg^|s>J4tsG5or-mO0D=xz1IMnapDa!wcmF!}EwZbaA)?3
(~obR3<qev^q}#VLrMbT`E7_nNI|vA9KWcCKJ$I`xbyu=c}fCq6B^-H-
iO41d2g97{<i;jWquGD$uG^0pTiiEm|e71iOj|LLv4eua(t17j~K*H0xCiF9f~5rkcu-
$C7wEEor~)MO4n25ARF|ZXmv9aNMlD9a{`)U7*0QIDaaFp)B7cEvDM11}q%LT)Sqsd9Ya$7nt47Q*WoV1Vp<~L1PI@<9Yd9h9yKa
4rhTE`fb5SC@wU+y{+DUX(=f7b=5d%6vZFpD3(%1(X1_Wx$_B)uN&<xG;6r80nxspnhTwuip~U(y6tO-Xe1&Qvq*fSkzDq*pw%}O
5Uxcb43&3$zLqYh!K>n%273tY_HEaJR2QiRbLX)RF&}27gAT8w3gbrlp*@mS#YN`M1JL&l%RsGJs?ppihHuI-ETc+kl*KG_k3ID}
pDzcUW~;_>qX2#>KYd<Kb>_wghh7|19Ttn(Mjt?H_LN)!f?cd2u>$o99r7kIE2siFKXwAgu_JM@;iu4A?)fH=>k<WroAM6A;R1Q7
<V{2zashWbm?JJRy<k;mVY&sx`j&#iEqSbs-p}OcKDSVvfbuBfPBrG@TZRwS)AA3iK(0$w#gh5Q(IzF*@NE^@*-
wY<4(EY4Dq!ML!z+}uPU2RBVwb6ECi7T>hQ9K=_iCz0nwMi=TxNJvL#-X_8j$L8Rmmi}UAkL-n_&$RgXYMtfrq}_SO`h|y471jt2
qh?x1u|xE%JLkw-Q0<>)D|v<`}(8((F^R7NojD!C-
CvYJ|Z9@;0|?sp4nYC6$@DqLB>YHqh!y1%%u3_ahJn$vZyZMzvLCAs1H~zE9Pn@F*xXSApQs{O=G1{nFA-
43ART0k3&1{<tZ|T$8Up>vm3h3>2HEK=BxQB)upnMe!IBiY7a7t9~4XvF}vCLCiCGr=&|`7l<}rL1PzsF1<e?U7L1M?UdqR5Rb%s
qwkhzdWd&Hs|5-
O@8v&(K(KQ?WDwq?g5Y#;vB2<At6Du`c7sw^DG=;NtChZ=?IlC7n+ky!#Jn@fhgG?_s*$#=dqA+O6(siLhae=z=cFX|kex(_Ud+Y
S#+pM?e?{60S}jyS*o#8=f*itLDs!mua;L>>JQNF!E?Ck8u@CfGqyVuG1#zn!#6GIsQU{MCk2_*fBQ*us4{9w|VA!Ak9y*>u8b|M
^!r(iDSZs^MjWoildm)inqM*>dfZcj~QSPMcUrh1pS!Uc}Tr4rz7PM<JjsUTiDkuyoV7t|?%kz3eh$v)7@_JP<?+{Ck<!}gL!;v7
@H3|+#7SIU|a@TMq5r<|kW@YdU*O*<ys(01oC=lyf1%;yun1en_PT?pj3SbS~j=V5t;##xqLC;B#1;MUUkQj@SxJ^!CEENe@l*(L
OXLR78xh`D>S}jvRxU7I(LY$I2P6pvJA_)DM)8?_x5`+$IVwu4u-
P$*=F9*ezD^OgHqWF~@#pP5ecn7RW%NuD%zygqJh3XPq;cP6~os}|JK*S*B+M@!$vu9A_PQ5f^g}FOd{kzy#gJ|DY(6}0n9~Q_h;
%X`nQRZ>b**B=KP2V=xSL-)zSO|h$uOP9ofYmGQklV#VDiR$$?(Ea%1>$;>>xy-4Vll{dgX%sU>LX5&Tf|}_4t-
hpM}xSbkp{gj0ljWiU4<*e2#BR}5KAZxqQqhEByKc0iqN%)J3+7SC_vm<=#GFmTfQ>gNd+R}agBF~@0dL~LyyD5Al7#k6do>M*AY
J>uV;Ojh{8ZVf_q^QIO4lz_YctH@Ce9tlY+w|g}w-
fweoABj}UPvaVH$c+8pkRo6J6+)+;1?0`$6B0pf|mG6cj*Ify5ylCFW%X>qr+r!Q_cJIkb(;CKo2TB!i>QehkdVxoLydWi@`@1QM
c2TwhU4HqkoCZQQaybOZfq9E~d0jq5-$?u-LOtp9Qc)6o~P5KtIU9}#FS3s>*3Jk9lzJ_2pNj{qO6(S6Mxa-
2cl{;cpBXM{Y<XWxZ@M_^)gu`U{orhPc%%N<%b|h9Co!8KOy#`vXQ9yVNU6W4Cc9B7NjR=CpDjtu;nnp_Vy$)L4s(|o%VL1Y!S1u
=m@H!EMB=cf%Ya{vQH$bVi3IuNyR$~2$<-Yk1s<^@9!z0guIC`z&PC<*sdXVcj1&8&8ldulOat`aMDrTI@NPlqb4-&T-
AHHe4Hh^5WD>!T@T!e7ANbVOl5OGNJh(%1?Zt|j}>z>~Pt?p1jc(d>v=A7lm@FrDKsU!4kN8DkuCFr{6w?M1!DImOsdWJ8`yDGg!
<(z%bcf|LMFVfI>Z3L<QNx@)a;fGkrCTFmbD#u;(YvP|8$sxQAN_}5};B7Q&{)Iehew!?@8hY)#>WS|gf9*%(OWpys{#k+H9n>$c
ln2f4Q2Av)ZuesdoBz3y2BmKTwSJ($u&Hn*f?<}tq<9k%h72!9o?jI|Xr$!DW)SON6cjcW79$i|<ZfXzRR)O%@kkyr@h?W-
HPPIuZUMFaRe@m(deoE5b(dk-LbXHUt+wO9Ch1=dUX!$w7+XQDA1W|xMK`K5veHRoTgi?WVm6YyRVIFDG6&stX&cD(BL#<T1?=k8
5IKi!WGU6Ew{%aX5{n<1Z4Y`Rwu4;%rWo<Fy@1UE{Rd0RVLK6rJRe-|KKXBl4B=(kVOLf{e^nY-OeB7+FouCe%r{KVNyiA(znCP2
VLKu0R)1`8&kXHiJsY(8i2{OMoQFWTCwrO<f=vV=1!nM*Mk3&VP<JW_IK{6b1QyH9z@ah&&yHI=B5|kD&ABx_LK(#RcLjxV5o`W=
gFK&FCZbT}fy*NC?`F4~)UyT`w7N?H!7XC9K+ly!aETyviCEM*(06%-
adDTq#ga*F@81fFty7?AEn>xzJ*~@SC|Ze7SUloo#=;;NCDs|-
P)U1b8U%v<R6$};5t|adCR>$}7(_&(pI5KZ^TMDaerkGtRp;e_UO!WS@QR%Xh$FI6N9_@T=;^X*XV&-
2;%7z&Z<>o!1?2j<f<vW<-Se59lO8uzh&UwqM?)zc@pF@VAL^1A4Qk!3z%aV_4+w^@C8cEmqp5aGe%W^TaB;WE4NP<$^-
K`!9tDM&MQmd9CQBN>%_O3bX!E!)?lId#>$!#tL8^Nd3@$9Tp-
p6@3(|#D+g3ZWE8<?mGpkyrumq&~h3d{>u^nNsJt3VkwuEYz6xxpeNgvi<nA`$XcTKt$B>Sam*jVwq2#Y@QgXn9CSTuukZu5Ls{L
=8-Jz6BL1GVl`%^NG)*nZU-C!IG&grUT$^_^0ci~Aa>BknSg>weY1v0^L2;pyCGqyxvO%%R5I>CN@|d-
of?Rnp|J9OQaHHFvCd7{cNA3HjVHDh@Gcq1O@nTs&a3L1>!9TF~o3)gXsrF9gJ{#8BxV2O<y!*S8(<U?VL`w}Dg-spdEov0mwW6N
gLZI8g1ExZ{U**%l9(Z3=oEZU?n~rND4Iis3Npd>Mw@i7+(TA$P>D8p#yy0I42UFu0?54#MEl<_R(ecTk1Um6(V1JD(3XQm^z)pw
=U*x=GX&-cCrPhD}uNxs{iBBpzubTi6U*{fDY-
5<T|$zqHgAHWNX}1r_erv#a7ijOL(uvtbL!^{A?D5;cZDB&25zTZlOHX>S*gnx53td2I!u9#fS}77s@W43?HmQY}Y*h5JDy9&4mf
VLOQRxProVbUhkojh7bck)@KtKs;`=A!wSy4p8bp6$o~qOVP85vt$T%5FzMqw=+llr@0LDOEGtXRR5)5u(Mb~7;H;OM>g!Fiq-
<BvsW-
J9XJ*7UyZauN|X|b|5lJllqMr2zMPYisDClZf>h?AAM+>>|7~ova0o+vERgF71qZ7%1>vwx&cPz$&^HQNS%+O_;t8XpHqBD8B*^u
of<v-24Qo^{A10G5QLRi7cWhfc*+|uTX%Oov1%-
48D~TDJl%7DRi6}JjYP)#KTm+W(<(@1k^|S&(7Ih3g<yA>pss)M1++mS;x{>At<Up)v6clpkax`8}AxE|xwfU|KVcRoi*M{jOF!G
?*vkDA()EK@iuZYPLVJJj^;90Y0`RGLs1yJfa1%g896a>My<d#sNvIG{}4j0cEZU<UxQZs1vyaGaV37ZeSPJZ9BnF<0ATwgqIbfl
meD|7{^{zt)}Yv~N^epOaF6}l@CgPy$1#s8SS7U`w4`+!ovRv_q8x)ee1LB36fpbrs(0Ri`z8-
#)Qwds}fI<_zb<oaLLw8PR=gv00M_p63bg$dviLO&A!YjSH6-A(FH5bFg6g`p+vmYXZ5FqDYGz}PPPp;td#c){p9pPD2t0=-
^TfVil17<%F?EzG-!2!zEP=0ps3NiQ~15zH)*>m>z;S*6|xhploBvxqq4m|NjxKk~#&jg;G-
4RZZP!C`hOhj2()ePkSF6LCn@5C5k}EaKuf#v6nNjCr8fZxtZsmA;ICIA87|=20Co#Gzkik@&6Ascp@Yr1>D$?-
Uf~m+~L$TuyzO!}7e8Lh{dk!QYfZ6n5Bco-cl9`u%5}g=s;lUQ~o*SWr3{F^1FR#;}0M7_xpV5B<&*FV}e0uX%R8#o_-MZ5f)G)`
g(l?^P2IQ6#U(FZe8^+QIs9R43v1;`e4RQF>=Ti$JfJ6(AO&NxnI9+gL;dqUb{;;m6`-vj>Z5<~N$)e`r-
rK+_cK;1rIP_nNGKG06%Qgkew%%HkE1yT|BuQnEm;S5+keO*bOOaG4x~MTDVHVUbhc!irao<!}g7CzBx8YpRj}l*1x9ha?e)bP%?
4@meFz!cBoxuPYd&PzDR+3{pf4QhYkBN3WZ_9_f~4bOov2P#rv=494WTNe>=~7&Q4V3p<;*%UvekFc^h)VL&%fY`p?Sx26uPm^&*
)(T&VQw}V?)Z?ZY)26Ej&stpPT-
J5103|7edqIV}^&^rJG8yd+gR6waWRh^Si1XJYULWOEY;;me~*+>WmgHmrP5Dad@BDO_xD;P|Kpx(SH6mK=sO+z1~+NfaQH=Twsx
K+-;Ct}d-
cJ>)IjEjvX*T(2xvKj$m{Xs!tL=$$wYGZ1&jKT;a3IlkR#a#Ts^f4Ja6avudZ3Tp&X&PE3CN+hC2tqGjt%|n|uHDu?bqhhMcN7Rh
6v5GQ1R)WEVi0*84B;J<OO$l|as*;+Qc#GRu!+Mf<rE?^pHSwbxF<H5eVatj8peQFn-vtsG%dxZ_GG04>BkUJ=o7c`O29kB=0+M}
H5Sy`qQEc~&0O@CpYMz%!qA1gRo>3+&=XrsA9<^TVjSqTRRLmL(-<tJEpJUSjtE4~j(vw!V$Q@?qkBJV`swkY*ER)+@l9VrKzt~-
i1AeJp*`jiA1=0;?4-3jxPe&PRUO=#0<?ZrI!$aj)w<MPpV6@0+;V#TtLR5GClWgp4322Vrf|=dU+t-XF--y^W@3lY5rbyTFdUTH
sX#Eixy{0Ywq_ZE;Y0}fj<BN|b9}MW@PxKj*f0Xr+NDZhG(V15!m6BfFvAEU3{Bt>+%WX)pyGvMm(eUV<F}C@*}JMdMl;r=<UzTA
4v1JReaQ)&s(81N#-
Wb^t=>~bZ_NX+I&L|HF+>phg`V%&;ytq=XvS`!)oxYn){LF;OpqtH#}Yv(gxuj`cOy;WJ{F|fqhN3>x*8pCohV~)ED?jAyftVK#2
&+o=(Xa7lR&Gz3J52m?)iK2d!Hu}K}d10%*5VCa?2-!RQnVRPHx8T-
u^V#L&o4_A_mQI;PNtenAm4@a}v!9CFg=%`xP9{ZJveNf;4D5mxx1-
yRO*ZNR!ad1Et<qAUF?2aJIZ3!+AsqO7(UOj(Fc}$E=q_x*W9nKmlP6>X@&U?@;FuLCAK*c55qh#0TbLNNRscxdpyRHRrRXCxRiH
le%WNg~%<GV;0tHkv?psY4ojiwK6z{))s7sbhX?XT8S_W<SuKqBT@gOO)Zz#wuU1>EK5OQNXr1UOjzEOfr^4(pTrOgD`T{0&;!u{
VkH$6I$FMtQ24fdhtxqtp<htpJQm4};nhewui>CoN&#Vb%WwojH@PVcCxVdnIP8){Dr0h!7)?v~5@?k^7=$Yh{E{!VoPa>s_vyI#
gO7fQ=BbN8T;U>}F}Y!qW(CH0kgAKq5XQGGLm1p4@2WbUh(Qx)ULd-d>=AU=qX{5YHgn+r`GYZ-*n-
ukPDx4`OeA73fH@V{b}Ax!$QVAb$T8@e!X!{Dr!a*{E#nXjpOrUyo<xM9hZl?re38qTJW5iN^JLH}uYfSQ<#z~#i{<OlWFiQ?0`8
0mL_X6%bI;d;Rs{uwwJmvc2*7OV*gUGJxufR!up)^<rjaJ+fm%fchTB>SXt%0a(#d&57?O6(>PKwFjM;-
>v_RYrVwDsWZg1&|Q0QV^EFGLjmqOyT`o-
E(BN@ZppjMLt!`*1u{F|(Frs~~P!2)yavS0N@Q^x2%FnVX5_kdc>3JmwO%tbK#SdQTysvMFX`Bhf$$RnB!2cZ=`KL~oYC_p@jI)@
qAVKNX8Ql*i+pi;5xNsJb=(|LNAJpUi0Ie`}"""


@cache
def _payloads() -> dict[int, ChassisPayload]:
    data = zlib.decompress(base64.b85decode(b"".join(_COMPRESSED_BANK.split())))
    offset = 0
    payloads: dict[int, ChassisPayload] = {}
    while offset < len(data):
        chassis_id, serialized_type_id = struct.unpack_from("<ii", data, offset)
        offset += 8
        static_block = data[offset:offset + 169]
        offset += 169
        name_end = data.index(0x4F, offset) + 1
        live_end = name_end + 205
        live_block = data[offset:live_end]
        offset = live_end
        payloads[chassis_id] = ChassisPayload(
            chassis_id=chassis_id,
            serialized_type_id=serialized_type_id,
            static_block=static_block,
            live_block=live_block,
        )
    if offset != len(data) or len(payloads) != 286:
        raise RuntimeError("Corrupt Dominions 6.36 pretender chassis payload bank")
    return payloads


def get_chassis_payload(chassis_id: int) -> ChassisPayload:
    try:
        return _payloads()[int(chassis_id)]
    except KeyError as exc:
        raise KeyError(f"No selectable Dominions 6.36 pretender payload for chassis {chassis_id}") from exc

