package repository

// codex_model_visibility_repo.go 实现「Codex 三方模型启用」的分组展示配置原子存储。
//
// 展示配置保存在 settings 表的一行里（键见 service.CodexModelVisibilitySettingKey），
// 值是带 revision 的 JSON。现有 SettingRepository 的 Set 是无条件 upsert，也没有版本列，
// 不能拿来做多节点下的乐观并发控制，所以这里单独实现一个窄接口：
//
//   - 在一个 ent 事务里对该键的行加 FOR UPDATE 行锁，读出当前配置；
//   - 比较 JSON 内的 revision 与调用方期望值，相等才写入，并把 revision 加一；
//   - 期望值为 0 表示「期望该行还不存在」，行已存在即冲突；
//   - 首次创建时并发撞到唯一键约束，同样按冲突返回，由页面重新加载后重试；
//   - 已存在但无法解析的行绝不覆盖，原样返回错误，交给人工处理。

import (
	"context"
	"errors"
	"fmt"
	"time"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/setting"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

type codexModelVisibilityStore struct {
	client *dbent.Client
	// lockRows 为 true 时读取配置行会带 FOR UPDATE 行锁。生产环境使用 PostgreSQL，必须为 true；
	// SQLite 不支持 FOR UPDATE（ent 会直接报错），只有单元测试才把它置为 false。
	lockRows bool
}

// NewCodexModelVisibilityStore 创建分组展示配置的原子存储。
func NewCodexModelVisibilityStore(client *dbent.Client) service.CodexModelVisibilityStore {
	return &codexModelVisibilityStore{client: client, lockRows: true}
}

// codexVisibilityWritePlan 是一次比较并交换的判定结果。
type codexVisibilityWritePlan struct {
	// created 为 true 表示需要新建行，否则更新已有行。
	created bool
	// value 是要写入 settings 表的序列化值。
	value string
	// written 是写入后的配置（revision 已递增）。
	written service.CodexModelVisibilityConfig
}

// planCodexModelVisibilityWrite 根据当前行的内容和调用方期望的 revision 判定如何写入，不访问数据库。
//
//   - found=false：期望 revision 必须为 0，否则说明配置在页面加载之后被别人删掉了，返回冲突；
//   - found=true：先严格解析当前内容，损坏时返回错误且不覆盖；期望 revision 为 0 或与当前 revision 不等都是冲突；
//   - 写入内容以 next 为准，revision 置为期望值加一，结构版本置为当前版本。
func planCodexModelVisibilityWrite(found bool, raw string, expectedRevision int64, next service.CodexModelVisibilityConfig) (codexVisibilityWritePlan, error) {
	if expectedRevision < 0 {
		return codexVisibilityWritePlan{}, service.ErrCodexModelVisibilityInvalid.WithCause(
			fmt.Errorf("negative expected revision %d", expectedRevision))
	}
	if found {
		current, err := service.ParseCodexModelVisibilityConfig(raw)
		if err != nil {
			// 已存在的行内容损坏：不得覆盖，原样上报。
			return codexVisibilityWritePlan{}, fmt.Errorf("existing codex model visibility is unreadable: %w", err)
		}
		if expectedRevision == 0 || current.Revision != expectedRevision {
			return codexVisibilityWritePlan{}, service.ErrCodexModelVisibilityConflict
		}
	} else if expectedRevision != 0 {
		return codexVisibilityWritePlan{}, service.ErrCodexModelVisibilityConflict
	}

	written := next.Clone()
	written.SchemaVersion = service.CodexModelVisibilitySchemaVersion
	written.Revision = expectedRevision + 1
	value, err := written.Marshal()
	if err != nil {
		return codexVisibilityWritePlan{}, service.ErrCodexModelVisibilityInvalid.WithCause(err)
	}
	return codexVisibilityWritePlan{created: !found, value: value, written: written}, nil
}

// CompareAndSwapCodexModelVisibility 在事务内加行锁、比较 revision 后写入。
func (s *codexModelVisibilityStore) CompareAndSwapCodexModelVisibility(
	ctx context.Context,
	groupID int64,
	expectedRevision int64,
	next service.CodexModelVisibilityConfig,
) (service.CodexModelVisibilityConfig, error) {
	if groupID <= 0 {
		return service.CodexModelVisibilityConfig{}, service.ErrCodexModelVisibilityInvalid.WithCause(
			fmt.Errorf("invalid group id %d", groupID))
	}
	// 先在事务外校验 next 本身是否合法，避免为一个必然失败的请求白白占用行锁。
	// 结构版本与 revision 由本函数统一覆盖，不采信调用方填的值，所以校验时也先按最终值归一。
	probe := next.Clone()
	probe.SchemaVersion = service.CodexModelVisibilitySchemaVersion
	probe.Revision = 0
	if _, err := probe.Marshal(); err != nil {
		return service.CodexModelVisibilityConfig{}, service.ErrCodexModelVisibilityInvalid.WithCause(err)
	}

	tx, err := s.client.Tx(ctx)
	if err != nil && !errors.Is(err, dbent.ErrTxStarted) {
		return service.CodexModelVisibilityConfig{}, err
	}
	client := s.client
	if err == nil {
		defer func() { _ = tx.Rollback() }()
		client = tx.Client()
	}

	key := service.CodexModelVisibilitySettingKey(groupID)
	query := client.Setting.Query().Where(setting.KeyEQ(key))
	if s.lockRows {
		query = query.ForUpdate()
	}
	row, err := query.Only(ctx)
	found := true
	raw := ""
	switch {
	case err == nil:
		raw = row.Value
	case dbent.IsNotFound(err):
		found = false
	default:
		return service.CodexModelVisibilityConfig{}, err
	}

	plan, err := planCodexModelVisibilityWrite(found, raw, expectedRevision, next)
	if err != nil {
		return service.CodexModelVisibilityConfig{}, err
	}
	if plan.created {
		if err := createCodexVisibilityRow(ctx, client, key, plan.value); err != nil {
			return service.CodexModelVisibilityConfig{}, err
		}
	} else if err := client.Setting.Update().
		Where(setting.KeyEQ(key)).
		SetValue(plan.value).
		SetUpdatedAt(time.Now()).
		Exec(ctx); err != nil {
		return service.CodexModelVisibilityConfig{}, err
	}
	if tx != nil {
		if err := tx.Commit(); err != nil {
			// 提交阶段撞到并发创建的唯一键约束时同样按冲突处理。
			return service.CodexModelVisibilityConfig{}, mapCodexVisibilityCreateError(err)
		}
	}
	return plan.written, nil
}

// createCodexVisibilityRow 新建配置行；并发首次创建撞到唯一键约束时返回冲突。
func createCodexVisibilityRow(ctx context.Context, client *dbent.Client, key, value string) error {
	err := client.Setting.Create().
		SetKey(key).
		SetValue(value).
		SetUpdatedAt(time.Now()).
		Exec(ctx)
	return mapCodexVisibilityCreateError(err)
}

// mapCodexVisibilityCreateError 把唯一键约束错误翻译为展示配置冲突，其它错误原样返回。
func mapCodexVisibilityCreateError(err error) error {
	if err == nil {
		return nil
	}
	if dbent.IsConstraintError(err) || isUniqueConstraintViolation(err) {
		return service.ErrCodexModelVisibilityConflict.WithCause(err)
	}
	return err
}

// DeleteCodexModelVisibility 删除分组展示配置；行不存在时视为成功。
func (s *codexModelVisibilityStore) DeleteCodexModelVisibility(ctx context.Context, groupID int64) error {
	if groupID <= 0 {
		return service.ErrCodexModelVisibilityInvalid.WithCause(fmt.Errorf("invalid group id %d", groupID))
	}
	_, err := s.client.Setting.Delete().
		Where(setting.KeyEQ(service.CodexModelVisibilitySettingKey(groupID))).
		Exec(ctx)
	return err
}
