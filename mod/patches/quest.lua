-- Azeroth Survivors mod: quests during a run (Lua 5.0, the WoW 1.12 UI).
-- A quest giver appears like a chest does; walking up to it takes the quest: reach every
-- waypoint before the time runs out, and the reward is the game's own item pick.
-- The exe side (quest.py) runs the commands set into the cvar "azsQuest", one at a time.

AZQ = {
	FIRST_AT = 120,      -- run seconds when the first quest giver appears
	COOLDOWN = 180,      -- seconds after a quest ended until the next giver appears
	DURATION = 150,      -- the quest's time limit, seconds
	POINTS = 5,          -- waypoints per quest
	POINT_MIN = 25,      -- waypoints are this many yards from the hero when the quest starts..
	POINT_MAX = 60,      -- ..up to this many
	POINT_GAP = 12,      -- yards between two waypoints at least
	GIVER_MIN = 20,      -- the giver appears this far from the hero..
	GIVER_MAX = 40,      -- ..up to this
	TOUCH = 4,           -- yards to reach a waypoint or the giver
	SHOW_END = 6,        -- seconds the tracker stays after the quest ended
	GIVER_DISPLAY = 146,   -- Creature\Ghost: a spirit; the game's data lacks most NPC models
	GIVER_STATUS = 5,    -- DIALOG_STATUS_AVAILABLE: the yellow "!"
	MARKER_DISPLAY = 10045, -- Creature\Wisp: a floating light over each waypoint
	MARKER_SCALE = 300,  -- percent
};

local TEXTS = {
	en = { title = "Scouting Run", points = "Points: %d/%d", left = "Time left: %s",
		done = "Completed", failed = "Failed", accepted = "Quest accepted: %s", completed = "%s completed." },
	ru = { title = "Разведка", points = "Точки: %d/%d", left = "Осталось: %s",
		done = "Выполнено", failed = "Провалено", accepted = "Получено задание: %s", completed = "Задание выполнено: %s" },
};

local CVAR = "azsQuest";
local SLOTS = 6;                 -- slot 0 the giver, 1..POINTS the waypoint markers
local ZOOM_YARDS = { 466.6667, 400, 333.3333, 266.6667, 200, 133.3333 };  -- minimap diameter per zoom
local CRUMBS = 400;

local state = "wait";            -- wait, giver, active, ended
local nextAt = nil;
local lastT = nil;
local giver = nil;               -- { x, y, z }
local points = {};               -- { x, y, z, done }
local reached = 0;
local endsAt = 0;
local hideAt = 0;
local crumbs = {};
local lastX, lastY = nil, nil;
local queue = {};
local statusAt = 0;

-- The game's language is not exposed to Lua, but the server's zone line carries translated
-- names: Cyrillic there means Russian (ponytail: Ukrainian reads Russian too).
local function L()
	local s = (AZS_ZONE.zsub or "") .. (AZS_ZONE.cname or "") .. (AZS_ZONE.rname or "");
	if ( string.find(s, "[\208\209]") ) then
		return TEXTS.ru;
	end
	return TEXTS.en;
end

-- The cvar store keeps numbers only, so a command is digits: the op, then each argument as
-- 7 digits holding value + 5000000 (quest.py reads them back).
local OPS = { S = "1", D = "2", G = "3", R = "4" };
local function Send(op, args)
	local s = OPS[op];
	for i = 1, table.getn(args or {}) do
		s = s .. string.format("%07d", args[i] + 5000000);
	end
	table.insert(queue, s);
end

-- Only while playing: the exe runs commands in the playing tick, so none waits across a death
-- into the next run.
local function Pump()
	if ( not queue[1] or AZS_RUN.ph ~= "playing" ) then
		return;
	end
	local ok, v = pcall(GetCVar, CVAR);
	if ( ok and v == "0" ) then
		SetCVar(CVAR, table.remove(queue, 1));
	end
end

function AZQ_Debug()
	local g = giver or {};
	DEFAULT_CHAT_FRAME:AddMessage(string.format("azq %s next=%s giver=%s,%s reached=%d queue=%d cvar=%s", state,
		tostring(nextAt), tostring(g.x), tostring(g.y), reached, table.getn(queue), tostring(GetCVar(CVAR))));
	for i = 1, table.getn(points) do
		local p = points[i];
		DEFAULT_CHAT_FRAME:AddMessage(string.format("  point %d %.1f,%.1f %s", i, p.x, p.y, tostring(p.done)));
	end
end

local function Dist(ax, ay, bx, by)
	local dx, dy = ax - bx, ay - by;
	return math.sqrt(dx * dx + dy * dy);
end

-- Distance from point p to the segment a..b: the hero may cover several yards between updates.
local function SegDist(px, py, ax, ay, bx, by)
	local dx, dy = bx - ax, by - ay;
	local len = dx * dx + dy * dy;
	local k = 0;
	if ( len > 0 ) then
		k = ((px - ax) * dx + (py - ay) * dy) / len;
		if ( k < 0 ) then k = 0; elseif ( k > 1 ) then k = 1; end
	end
	return Dist(px, py, ax + k * dx, ay + k * dy);
end

local function Spawn(slot, x, y, z, display, npcflags, scale)
	local o = math.atan2(AZS_RUN.py - y, AZS_RUN.px - x);
	if ( o < 0 ) then
		o = o + 2 * math.pi;
	end
	z = z or AZS_RUN.pz;
	Send("S", { slot, math.floor(x * 10 + 0.5), math.floor(y * 10 + 0.5), math.floor(z * 10 + 0.5),
		math.floor(o * 1000), display, npcflags, scale });
end

-- Spots the hero can walk to: where they have been, the chests, the guardians' lairs.
local function Spots()
	local out = {};
	for i = 1, table.getn(crumbs) do
		table.insert(out, crumbs[i]);
	end
	for i = 1, table.getn(AZS_CHESTS) do
		local c = AZS_CHESTS[i];
		table.insert(out, { x = c.x, y = c.y, z = c.z });
	end
	for i = 1, table.getn(AZS_LAIRS) do
		local c = AZS_LAIRS[i];
		if ( c.x ) then
			table.insert(out, { x = c.x, y = c.y, z = c.z });
		end
	end
	return out;
end

-- n spots lo..hi yards from the hero, POINT_GAP apart; looser bounds when too few qualify.
local function Pick(n, lo, hi)
	local spots = Spots();
	for i = table.getn(spots), 2, -1 do
		local j = math.random(i);
		spots[i], spots[j] = spots[j], spots[i];
	end
	local px, py = AZS_RUN.px, AZS_RUN.py;
	local picked = {};
	local bounds = { { lo, hi }, { lo * 0.5, hi * 1.5 }, { 8, 100000 } };
	for b = 1, table.getn(bounds) do
		for i = 1, table.getn(spots) do
			if ( table.getn(picked) >= n ) then
				return picked;
			end
			local s = spots[i];
			local d = Dist(s.x, s.y, px, py);
			local free = d >= bounds[b][1] and d <= bounds[b][2];
			for k = 1, table.getn(picked) do
				if ( free and Dist(s.x, s.y, picked[k].x, picked[k].y) < AZQ.POINT_GAP ) then
					free = false;
				end
			end
			if ( free ) then
				table.insert(picked, { x = s.x, y = s.y, z = s.z });
			end
		end
	end
	-- ponytail: a hero who never moved has no crumbs; a ring around them may hit a wall
	while ( table.getn(picked) < n ) do
		local a = math.random() * 2 * math.pi;
		local d = lo + math.random() * (hi - lo);
		table.insert(picked, { x = px + d * math.cos(a), y = py + d * math.sin(a), z = AZS_RUN.pz });
	end
	return picked;
end

-- Tracker under the minimap, as the stock quest watch.
local tracker = CreateFrame("Frame", "AZQTracker", UIParent);
tracker:SetWidth(220);
tracker:SetHeight(60);
tracker:SetPoint("TOPRIGHT", MinimapCluster, "BOTTOMRIGHT", -8, -4);
tracker:Hide();
local function Line(font, y)
	local f = tracker:CreateFontString(nil, "ARTWORK", font);
	f:SetPoint("TOPRIGHT", tracker, "TOPRIGHT", 0, y);
	f:SetJustifyH("RIGHT");
	return f;
end
local titleText = Line("GameFontNormal", 0);
local pointsText = Line("GameFontHighlightSmall", -16);
local timeText = Line("GameFontHighlightSmall", -30);

local function Clock(s)
	if ( s < 0 ) then
		s = 0;
	end
	s = math.ceil(s);
	return string.format("%d:%02d", math.floor(s / 60), math.mod(s, 60));
end

local function ShowTracker(t)
	local l = L();
	titleText:SetText(l.title);
	pointsText:SetText(string.format(l.points, reached, AZQ.POINTS));
	if ( state == "active" ) then
		timeText:SetText(string.format(l.left, Clock(endsAt - t)));
		if ( endsAt - t < 30 ) then
			timeText:SetTextColor(1, 0.3, 0.3);
		else
			timeText:SetTextColor(1, 1, 1);
		end
	end
	tracker:Show();
end

-- Minimap pins: the giver's "!" and the waypoints as gold dots, held at the rim when out of
-- range. Plain colour and text: the game's data has none of the stock quest or raid icons.
local pins = {};
for i = 0, SLOTS - 1 do
	local f = CreateFrame("Frame", nil, Minimap);
	f:SetFrameLevel(Minimap:GetFrameLevel() + 5);
	f:SetWidth(8);
	f:SetHeight(8);
	if ( i == 0 ) then
		local bang = f:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge");
		bang:SetPoint("CENTER", f, "CENTER", 0, 0);
		bang:SetText("!");
	else
		local edge = f:CreateTexture(nil, "ARTWORK");
		edge:SetAllPoints(f);
		edge:SetTexture(0, 0, 0);
		local dot = f:CreateTexture(nil, "OVERLAY");
		dot:SetPoint("TOPLEFT", f, "TOPLEFT", 1, -1);
		dot:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -1, 1);
		dot:SetTexture(1, 0.82, 0);
	end
	f:Hide();
	pins[i] = f;
end

local function Pin(i, x, y)
	local f = pins[i];
	if ( not x ) then
		f:Hide();
		return;
	end
	local zoom = (Minimap:GetZoom() or 0) + 1;
	local size = Minimap:GetWidth();
	local perYard = size / (ZOOM_YARDS[zoom] or ZOOM_YARDS[1]);
	local dx, dy = -(y - AZS_RUN.py) * perYard, (x - AZS_RUN.px) * perYard;
	local d = math.sqrt(dx * dx + dy * dy);
	local rim = size / 2 - 6;
	if ( d > rim ) then
		dx, dy = dx * rim / d, dy * rim / d;
	end
	f:ClearAllPoints();
	f:SetPoint("CENTER", Minimap, "CENTER", dx, dy);
	f:Show();
end

local function UpdatePins()
	if ( state == "giver" ) then
		Pin(0, giver.x, giver.y);
	else
		Pin(0);
	end
	for i = 1, SLOTS - 1 do
		local p = points[i];
		if ( state == "active" and p and not p.done ) then
			Pin(i, p.x, p.y);
		else
			Pin(i);
		end
	end
end

local function ClearWorld()
	for i = 0, SLOTS - 1 do
		Send("D", { i });
	end
end

local function Reset()
	state, giver, points, reached, crumbs, queue = "wait", nil, {}, 0, {}, {};
	nextAt, lastX, lastY = AZQ.FIRST_AT, nil, nil;
	tracker:Hide();
	ClearWorld();
end

local function Finish(t, ok)
	state = "ended";
	hideAt = t + AZQ.SHOW_END;
	nextAt = t + AZQ.COOLDOWN;
	local l = L();
	if ( ok ) then
		timeText:SetText(l.done);
		timeText:SetTextColor(0.2, 1, 0.2);
		PlaySound("QUESTCOMPLETED");
		UIErrorsFrame:AddMessage(string.format(l.completed, l.title), 1, 1, 0);
		Send("R");
	else
		timeText:SetText(l.failed);
		timeText:SetTextColor(1, 0.2, 0.2);
		for i = 1, SLOTS - 1 do
			if ( points[i] and not points[i].done ) then
				Send("D", { i });
			end
		end
	end
end

local function Accept(t)
	Send("D", { 0 });
	PlaySound("QUESTADDED");
	points = Pick(AZQ.POINTS, AZQ.POINT_MIN, AZQ.POINT_MAX);
	for i = 1, table.getn(points) do
		local p = points[i];
		Spawn(i, p.x, p.y, p.z, AZQ.MARKER_DISPLAY, 0, AZQ.MARKER_SCALE);
	end
	reached, endsAt, state = 0, t + AZQ.DURATION, "active";
	local l = L();
	UIErrorsFrame:AddMessage(string.format(l.accepted, l.title), 1, 1, 0);
end

local function Step(t, x, y, ax, ay)
	if ( state == "wait" and nextAt and t >= nextAt ) then
		local s = Pick(1, AZQ.GIVER_MIN, AZQ.GIVER_MAX)[1];
		giver = s;
		Spawn(0, s.x, s.y, s.z, AZQ.GIVER_DISPLAY, 2, 0);
		Send("G", { 0, AZQ.GIVER_STATUS });
		statusAt = t + 2;
		state = "giver";
	elseif ( state == "giver" ) then
		if ( SegDist(giver.x, giver.y, ax, ay, x, y) <= AZQ.TOUCH ) then
			Accept(t);
		elseif ( t >= statusAt ) then
			Send("G", { 0, AZQ.GIVER_STATUS });  -- the client may ask the server, which knows no such giver
			statusAt = t + 3;
		end
	elseif ( state == "active" ) then
		for i = 1, table.getn(points) do
			local p = points[i];
			if ( not p.done and SegDist(p.x, p.y, ax, ay, x, y) <= AZQ.TOUCH ) then
				p.done = true;
				reached = reached + 1;
				Send("D", { i });
				if ( reached < AZQ.POINTS ) then
					PlaySound("MapPing");
				end
			end
		end
		if ( reached >= AZQ.POINTS ) then
			Finish(t, true);
		elseif ( t >= endsAt ) then
			Finish(t, false);
		end
	elseif ( state == "ended" and t >= hideAt ) then
		tracker:Hide();
		state = "wait";
	end
	if ( state == "active" or state == "ended" ) then
		ShowTracker(t);
	end
end

local function OnUpdate()
	Pump();
	local t = AZS_GetRunTime();
	local ph = AZS_RUN.ph;
	if ( not t or not AZS_RUN.px ) then
		return;
	end
	if ( lastT and t < lastT - 1 ) then
		Reset();  -- a new run
	end
	lastT = t;
	if ( ph == "dead" or ph == "victory" ) then
		if ( state ~= "wait" or tracker:IsShown() ) then
			state = "wait";
			nextAt = nil;
			tracker:Hide();
			ClearWorld();
		end
		UpdatePins();
		return;
	end
	if ( not nextAt and state == "wait" ) then
		nextAt = AZQ.FIRST_AT;
	end
	local x, y = AZS_RUN.px, AZS_RUN.py;
	local ax, ay = lastX or x, lastY or y;
	lastX, lastY = x, y;
	if ( ph == "playing" ) then
		local c = crumbs[table.getn(crumbs)];
		if ( not c or Dist(c.x, c.y, x, y) >= 4 ) then
			table.insert(crumbs, { x = x, y = y, z = AZS_RUN.pz });
			if ( table.getn(crumbs) > CRUMBS ) then
				table.remove(crumbs, 1);
			end
		end
		Step(t, x, y, ax, ay);
	end
	UpdatePins();
end

local frame = CreateFrame("Frame", "AZQFrame");
frame:SetScript("OnUpdate", OnUpdate);
