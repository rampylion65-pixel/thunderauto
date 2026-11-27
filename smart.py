import random,re,unicodedata,itertools
from datetime import datetime,timedelta,timezone
IST=timezone(timedelta(hours=5,minutes=30))
def norm(s): return ' '.join(re.findall(r'\w+',unicodedata.normalize('NFKC',s).casefold()))
PATTERNS=['{topic}: {angle} for {goal}','Exploring {topic} Through {angle}','{angle} and {topic}: A Path to {goal}','Finding {goal} Through {topic}','{topic} and the Art of {angle}','A Reflection on {topic} and {angle}','Discover {angle} with {topic}','{topic}: A Fresh Perspective on {angle}','From {angle} to {goal} with {topic}','Understanding {topic}: {angle} and {goal}']
BUDDHA=['Mindfulness','Compassion','Inner Peace','Patience','Letting Go','Kindness','Meditation','Self Awareness','Wisdom','Gratitude','Acceptance','Balance','Simplicity','Generosity','Reflection','Non Attachment','Understanding','Calm','Forgiveness','Right Intention']
GENERAL=['New Perspectives','Key Ideas','Everyday Insights','Meaningful Details','Fresh Discoveries','Important Themes','Thoughtful Reflections','Interesting Perspectives','Essential Concepts','Practical Insights','Creative Ideas','Useful Observations','Exploring Ideas','Closer Look','Daily Inspiration']
GOALS=['Everyday Life','Greater Understanding','Personal Growth','A Fresh Perspective','Meaningful Reflection','Daily Inspiration','A Clearer Mind','New Insights','Thoughtful Living','Deeper Awareness']
def metadata(topic,names,language='English',used=(),extra=()):
 if language!='English': raise ValueError('Internal engine currently supports English only')
 topic=' '.join(topic.split())
 if not 1<=len(topic)<=60: raise ValueError('Topic must be 1–60 characters')
 angles=(BUDDHA if any(x in topic.lower() for x in ['buddha','buddhist','buddhism']) else GENERAL)+list(extra)
 seen=set(used); candidates=[]
 for p,a,g in itertools.product(PATTERNS,angles,GOALS):
  t=p.format(topic=topic,angle=a,goal=g)
  if len(t)<=99 and norm(t) not in seen: candidates.append((t,a))
 # Additional compositional structures, with distinct topic, angle and goal.
 openings=['A Reflection on','A Guide to','A Closer Look at','Exploring','Understanding','Discovering','Learning About','Considering','Finding Meaning in','A Journey Through','An Introduction to','A Perspective on','Thinking About','Making Sense of','Looking Beyond','Insights into','Lessons in','Reflections on','A Moment for','A Path Through']
 endings=['in Daily Life','for Everyday Reflection','with a Calm Mind','for Personal Growth','in Practice','for a New Perspective','for Greater Awareness','in Modern Life','for Thoughtful Living','for Deeper Understanding']
 for lead,a,g,end in itertools.product(openings,angles,GOALS,endings):
  t=f'{lead} {topic}: {a} and {g} {end}'
  if len(t)<=99 and norm(t) not in seen: candidates.append((t,a))
 random.shuffle(candidates); out=[]
 for t,a in candidates:
  k=norm(t)
  if k in seen: continue
  seen.add(k); tags=['#'+re.sub(r'[^\w]','',x) for x in [topic,a,'Shorts']]; out.append({'title':t,'description':f'{t}. Explore this perspective on {topic}.\n\n'+ ' '.join(dict.fromkeys(tags))})
  if len(out)==len(names): return out
 raise ValueError('Title combinations exhausted. Add more relevant knowledge phrases.')
def schedule(count,daily,start,now=None,occupied=(),mode='new'):
 if not 1<=daily<=5 or count<1: raise ValueError('Invalid count')
 now=now or datetime.now(IST); day=max(datetime.fromisoformat(start).date(),now.date()); out=[]; occupied=list(occupied); by_day={}
 for x in occupied: by_day.setdefault(x.date(),[]).append(x)
 if mode=='instant': return [now.strftime('%Y-%m-%dT%H:%M')]*count
 if mode=='continue' and occupied: day=max(day,max(occupied).date()+timedelta(days=1))
 while len(out)<count:
  existing=by_day.get(day,[]); morning=daily//2 if daily>1 else 1
  slots=[]
  for hour,n in [(6,morning),(17,daily-morning)]:
   existing_n=sum(x.hour==hour for x in existing)
   minutes=[m for m in range(60) if not any(x.hour==hour and x.minute==m for x in existing)]
   for m in sorted(random.sample(minutes,min(len(minutes),max(0,n-existing_n)))):
    x=datetime.combine(day,datetime.min.time(),IST).replace(hour=hour,minute=m)
    if x>now: slots.append(x)
  out.extend(slots[:min(count-len(out),max(0,daily-len(existing)))])
  day+=timedelta(days=1)
 return [x.strftime('%Y-%m-%dT%H:%M') for x in out]
