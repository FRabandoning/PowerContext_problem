// 谜语 + 冷知识数据集
// 用于灌入 seekdb，作为「谜海拾贝」应用的知识底座

export const RIDDLES = [
  { id: "r001", text: "身穿绿衣裳，肚里水汪汪，生的儿子多，个个黑脸膛。（打一水果）", answer: "西瓜", category: "食物", difficulty: 1 },
  { id: "r002", text: "小小虫儿真奇妙，白天睡觉夜里忙，点上灯笼找食吃。（打一昆虫）", answer: "萤火虫", category: "动物", difficulty: 1 },
  { id: "r003", text: "有面没有口，有脚没有手，虽有四只脚，自己不会走。（打一物品）", answer: "桌子", category: "物品", difficulty: 1 },
  { id: "r004", text: "看不见，摸不着，跑得快，又没脚，一去永远不回头。（打一事物）", answer: "时间", category: "自然", difficulty: 2 },
  { id: "r005", text: "头戴红缨帽，身穿绿战袍，说话像喷壶，走路像舞蹈。（打一动物）", answer: "鹦鹉", category: "动物", difficulty: 2 },
  { id: "r006", text: "白胖娃娃泥里卧，腰身细细心眼多。（打一蔬菜）", answer: "莲藕", category: "食物", difficulty: 2 },
  { id: "r007", text: "一口咬掉牛尾巴。（打一字）", answer: "告", category: "字谜", difficulty: 2 },
  { id: "r008", text: "上边毛，下边毛，中间一颗黑葡萄。（打一人体器官）", answer: "眼睛", category: "身体", difficulty: 1 },
  { id: "r009", text: "耳朵长，尾巴短，红眼睛，白毛衫，三瓣嘴儿胆子小。（打一动物）", answer: "兔子", category: "动物", difficulty: 1 },
  { id: "r010", text: "千条线，万条线，掉到水里看不见。（打一自然现象）", answer: "雨", category: "自然", difficulty: 1 },
  { id: "r011", text: "小小一间房，只有一扇窗，唱歌又演戏，天天翻花样。（打一物品）", answer: "电视机", category: "物品", difficulty: 1 },
  { id: "r012", text: "麻屋子，红帐子，里面住着白胖子。（打一食物）", answer: "花生", category: "食物", difficulty: 1 },
  { id: "r013", text: "身披黑白衣，样子真神气，人人都爱它，圆滚滚爱吃竹。（打一动物）", answer: "大熊猫", category: "动物", difficulty: 1 },
  { id: "r014", text: "一人一张口，口下长只手。（打一字）", answer: "拿", category: "字谜", difficulty: 2 },
  { id: "r015", text: "有时落在山腰，有时挂在树梢，有时像面圆镜，有时像把镰刀。（打一天体）", answer: "月亮", category: "自然", difficulty: 1 },
  { id: "r016", text: "红公鸡，绿尾巴，身子钻在土底下，你要捉住它，揪住尾巴用力拔。（打一蔬菜）", answer: "胡萝卜", category: "食物", difficulty: 1 },
  { id: "r017", text: "团结劳动是能手，家家住着小门楼，个个开着糖坊铺，日日夜夜忙不休。（打一昆虫）", answer: "蜜蜂", category: "动物", difficulty: 2 },
  { id: "r018", text: "一只大公鸡，天天报时来，不喝水来不吃米，只催人们早早起。（打一物品）", answer: "闹钟", category: "物品", difficulty: 1 },
  { id: "r019", text: "十个小伙伴，分成两个班，互相团结紧，倒海又移山。（打一人体部位）", answer: "手", category: "身体", difficulty: 1 },
  { id: "r020", text: "身长近一丈，鼻在头顶上，腹白背青黑，安家在海洋。（打一动物）", answer: "海豚", category: "动物", difficulty: 2 },
  { id: "r021", text: "小时青来老来红，立夏时节招顽童，手舞竹竿请下地，吃完两手红彤彤。（打一水果）", answer: "桑葚", category: "食物", difficulty: 2 },
  { id: "r022", text: "两撇小胡子，尖嘴尖牙齿，贼头又贼脑，夜晚干坏事。（打一动物）", answer: "老鼠", category: "动物", difficulty: 1 },
  { id: "r023", text: "一物生来强，每天织网忙，织完静静坐，专等蚊虫撞。（打一动物）", answer: "蜘蛛", category: "动物", difficulty: 1 },
  { id: "r024", text: "长得像竹不是竹，周身有节不太粗，又是紫来又是绿，只吃生来不吃熟。（打一植物）", answer: "甘蔗", category: "食物", difficulty: 2 },
];

export const FACTS = [
  { id: "f001", text: "蜂蜜几乎永远不会变质，考古学家在古埃及金字塔里发现过三千多年仍可食用的蜂蜜。", category: "科学" },
  { id: "f002", text: "章鱼有三颗心脏，其中两颗专门给鳃供血，第三颗负责给全身其他部位供血。", category: "动物" },
  { id: "f003", text: "人的胃酸强度足以溶解刀片，但胃壁会不断自我更新来保护自己。", category: "人体" },
  { id: "f004", text: "树懒憋气的时间比海豚还长，它们可以在水下屏住呼吸长达四十分钟。", category: "动物" },
  { id: "f005", text: "死海不是海，而是一个咸水湖，它的盐度大约是普通海水的十倍。", category: "地理" },
  { id: "f006", text: "一汤匙中子星物质的重量，大约相当于地球上的一座山那么重。", category: "科学" },
  { id: "f007", text: "鸭嘴兽是少数会下蛋的哺乳动物之一，而且雄性鸭嘴兽的后足还长有毒刺。", category: "动物" },
  { id: "f008", text: "埃及艳后克利奥帕特拉生活的年代，离人类登月的时间，比离金字塔修建的时间更近。", category: "历史" },
  { id: "f009", text: "木星上的大红斑是一场持续了至少数百年、而且比整个地球还大的巨型风暴。", category: "科学" },
  { id: "f010", text: "猫的味蕾尝不出甜味，所以它们对甜食其实毫无兴趣。", category: "动物" },
  { id: "f011", text: "人类是唯一会脸红、并且会因为情绪而流泪的动物。", category: "人体" },
  { id: "f012", text: "澳大利亚的国土面积比冥王星的表面积还要大。", category: "地理" },
  { id: "f013", text: "光从太阳表面出发，大约需要八分二十秒才能到达地球。", category: "科学" },
  { id: "f014", text: "海马是少数由雄性怀孕并生育后代的动物。", category: "动物" },
  { id: "f015", text: "香蕉在植物学上其实是浆果，而草莓严格来说并不是浆果。", category: "生活" },
  { id: "f016", text: "大象是少数无法跳跃的哺乳动物之一。", category: "动物" },
  { id: "f017", text: "你身体里的原子大多来自爆炸的恒星，所以可以说你是由星尘组成的。", category: "科学" },
  { id: "f018", text: "撒哈拉沙漠曾经是一片郁郁葱葱的草原，大约六千年前才逐渐沙漠化。", category: "地理" },
  { id: "f019", text: "蜂鸟是唯一能够向后飞行的鸟。", category: "动物" },
  { id: "f020", text: "成年人体内的血管如果全部连接起来，可以绕地球两圈半以上。", category: "人体" },
];

export function riddleDocuments() {
  return RIDDLES.map((r) => ({
    id: r.id,
    document: `${r.text}（分类：${r.category}）`,
    metadata: { type: "riddle", answer: r.answer, category: r.category, difficulty: r.difficulty },
  }));
}

export function factDocuments() {
  return FACTS.map((f) => ({
    id: f.id,
    document: f.text,
    metadata: { type: "fact", category: f.category },
  }));
}

export function allDocuments() {
  return [...riddleDocuments(), ...factDocuments()];
}
